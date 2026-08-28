// Page -> agent-visible HTML, using the browser's own answers.
//
// This replaces a heuristic that recomputed what the engine already knows:
// element visibility was re-derived from `display` / `visibility` / `opacity`
// by hand, and "which block is the main list" was scored with tuned constants.
// `Element.checkVisibility()` is the engine's own verdict (it accounts for
// `content-visibility` and `opacity` chains that a manual walk misses), and
// grouping by structural signature needs no weights at all.
//
// Contract, unchanged from what `simphtml.py` consumes:
//   * returns `document.body`-rooted `outerHTML`, or plain text when `textOnly`
//   * prepends `<!--btap-base:HREF-->`
//   * appends `<!--btap-offscreen:N scrollY:S viewH:V docH:D-->` when anything
//     rendered was dropped for being outside the +/-5000px window
//   * an empty document returns a `data-btap-state="empty-document"` root
//   * `<iframe>` becomes `div[data-tag="iframe"]` with `data-iframe-content`
//   * form state is written onto the clone as attributes
//
// The file must end with a bare `pageOutline;` -- never a call. The call site
// appends `return pageOutline(...)`, so a trailing call would run the whole
// analysis twice per request (see AGENTS.md section 1).

function pageOutline(textOnly = false) {
  const RANGE_PX = 5000;
  const SKIP_TAGS = new Set([
    'SCRIPT', 'STYLE', 'NOSCRIPT', 'META', 'LINK', 'COLGROUP',
    'COL', 'TEMPLATE', 'PARAM', 'SOURCE', 'TRACK',
  ]);
  const SKIP_IDS = new Set(['btap-indicator']);

  // Page analysis must not pollute an active console capture.
  const saved = {
    log: console.log, warn: console.warn, info: console.info, debug: console.debug,
  };
  console.log = console.warn = console.info = console.debug = () => {};

  let offscreen = 0;

  // The engine's verdict, not a re-derivation of it. `checkVisibility` covers
  // `content-visibility: hidden` and inherited opacity, which a per-element
  // style read reports as visible.
  //
  // These three option names are Chrome 121; the method itself is 105, and
  // before 121 the same members were spelled `checkOpacity` and
  // `checkVisibilityCSS`. Unknown members of a WebIDL dictionary are dropped
  // without an error, so on an older engine this call still returns a verdict --
  // one computed without the opacity and visibility checks, which puts hidden
  // text back in front of the model with nothing anywhere reporting a problem.
  // `manifest.json` therefore declares 121, and
  // `tests/test_distribution_contract.py` derives that floor from this file.
  const renderState = (el) => {
    try {
      if (!el.checkVisibility({
        contentVisibilityAuto: true,
        opacityProperty: true,
        visibilityProperty: true,
      })) return { rendered: false, rect: null };
    } catch (_) {
      const s = window.getComputedStyle(el);
      if (s.display === 'none' || s.visibility === 'hidden') {
        return { rendered: false, rect: null };
      }
      if (parseFloat(s.opacity) <= 0) return { rendered: false, rect: null };
    }
    const r = el.getBoundingClientRect();
    return { rendered: r.width > 1 && r.height > 1, rect: r };
  };

  // Viewport-relative, so this is "near the current scroll position", not the
  // document. Content beyond it is dropped to keep the payload small, but the
  // drop is counted so the caller can say "scroll and re-scan" instead of
  // concluding the element does not exist.
  const inRange = (r) => {
    return Math.abs(r.left) < RANGE_PX && Math.abs(r.top) < RANGE_PX;
  };

  // Live form state lives in properties, not attributes, so a clone loses it.
  const carryState = (src, clone) => {
    const tag = src.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') {
      if (src.value) clone.setAttribute('value', src.value);
      if ((src.type === 'radio' || src.type === 'checkbox') && src.checked) {
        clone.setAttribute('checked', '');
      }
    } else if (tag === 'SELECT' && src.value) {
      clone.setAttribute('data-selected', src.value);
    }
    try {
      if (src.matches(':-webkit-autofill')) {
        clone.setAttribute('data-autofilled', 'true');
        if (!src.value) clone.setAttribute('value', '[protected autofill value]');
      }
    } catch (_) { /* :-webkit-autofill is unsupported outside Chromium */ }
  };

  const copy = (src) => {
    if (!src) return null;
    if (src.nodeType === Node.COMMENT_NODE) return null;
    if (src.nodeType === Node.TEXT_NODE) {
      return src.textContent.trim() ? src.cloneNode(false) : null;
    }
    if (src.nodeType !== Node.ELEMENT_NODE) return null;
    if (SKIP_TAGS.has(src.tagName) || (src.id && SKIP_IDS.has(src.id))) return null;

    // An iframe's content is cross-origin more often than not, so record the
    // source and let `simphtml` turn the placeholder back into an <iframe>.
    if (src.tagName === 'IFRAME') {
      if (!renderState(src).rendered) return null;
      const box = document.createElement('div');
      box.setAttribute('data-tag', 'iframe');
      box.setAttribute('data-iframe-content', src.src || '');
      return box;
    }

    const state = renderState(src);
    const shown = state.rendered;
    if (shown && !inRange(state.rect)) { offscreen += 1; return null; }
    if (!shown && src.getAttribute('aria-hidden') === 'true') return null;

    const clone = src.cloneNode(false);
    carryState(src, clone);

    // Text under a hidden element is hidden text, so it does not count as
    // surviving content -- only a kept *element* does. Counting text nodes here
    // leaked the contents of every `display:none` block, because their text
    // clones fine and made the parent look non-empty.
    let keptElements = 0;
    let keptAny = 0;
    for (const child of src.childNodes) {
      const sub = copy(child);
      if (!sub) continue;
      clone.appendChild(sub);
      keptAny += 1;
      if (sub.nodeType === Node.ELEMENT_NODE) keptElements += 1;
    }

    // A hidden element with no surviving descendant element carries nothing; one
    // that still holds visible descendants is structure the agent needs.
    if (!shown && keptElements === 0) return null;
    if (shown && keptAny === 0 && !src.textContent.trim()) {
      const interactive = src.matches(
        'input, button, select, textarea, a[href], img, [role], [onclick], [tabindex]',
      );
      if (!interactive) return null;
    }
    return clone;
  };

  try {
    const body = document.body;
    if (!body) {
      if (textOnly) return '';
      const root = document.createElement('body');
      root.setAttribute('data-btap-state', 'empty-document');
      root.insertAdjacentHTML('afterbegin', '<!--btap-base:' + location.href + '-->');
      return root.outerHTML;
    }

    const root = copy(body) || document.createElement('body');
    if (textOnly) return root.textContent;

    root.insertAdjacentHTML('afterbegin', '<!--btap-base:' + location.href + '-->');
    if (offscreen > 0) {
      root.insertAdjacentHTML(
        'beforeend',
        '<!--btap-offscreen:' + offscreen
        + ' scrollY:' + Math.round(window.scrollY)
        + ' viewH:' + window.innerHeight
        + ' docH:' + document.documentElement.scrollHeight + '-->',
      );
    }
    return root.outerHTML;
  } finally {
    console.log = saved.log;
    console.warn = saved.warn;
    console.info = saved.info;
    console.debug = saved.debug;
  }
}

pageOutline;
