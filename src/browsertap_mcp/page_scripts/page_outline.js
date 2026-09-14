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
    if (tag === 'INPUT') {
      clone.setAttribute('value', src.value || '');
      if (src.type === 'radio' || src.type === 'checkbox') {
        if (src.checked) clone.setAttribute('checked', '');
        else clone.removeAttribute('checked');
      }
    } else if (tag === 'TEXTAREA') {
      clone.removeAttribute('value');
      clone.textContent = src.value || '';
    } else if (tag === 'SELECT') {
      clone.setAttribute('data-selected', src.value || '');
    } else if (tag === 'OPTION') {
      if (src.selected) clone.setAttribute('selected', '');
      else clone.removeAttribute('selected');
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
      for (const name of ['id', 'name', 'title']) {
        if (src.getAttribute(name)) box.setAttribute(name, src.getAttribute(name));
      }
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
    for (const child of (src.tagName === 'TEXTAREA' ? [] : src.childNodes)) {
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

// Locators describe this observation, not persistent element identities. Input
// tools resolve and validate them again before dispatch. No live DOM is changed.
function pageTargets(limit = 80) {
  const targets = [];
  const frames = [];
  let truncated = false;
  let visited = 0;
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  const selectorFor = (element, root) => {
    const unique = selector => {
      const matches = root.querySelectorAll(selector);
      return matches.length === 1 && matches[0] === element;
    };
    if (element.id) {
      const selector = '#' + CSS.escape(element.id);
      if (unique(selector)) return selector;
    }
    const tag = CSS.escape(element.localName);
    for (const attr of ['name', 'data-testid']) {
      const value = element.getAttribute(attr);
      if (value) {
        const selector = `${tag}[${attr}="${CSS.escape(value)}"]`;
        if (unique(selector)) return selector;
      }
    }
    const parts = [];
    for (let node = element; node && node.nodeType === 1; node = node.parentElement) {
      let index = 1;
      for (let sibling = node.previousElementSibling; sibling; sibling = sibling.previousElementSibling) {
        if (sibling.localName === node.localName) index++;
      }
      parts.unshift(`${CSS.escape(node.localName)}:nth-of-type(${index})`);
      const selector = parts.join(' > ');
      if (unique(selector)) return selector;
    }
    return null;
  };
  const inherited = (element, selector) => {
    for (let node = element; node; node = node.parentElement || node.getRootNode().host) {
      if (node.matches(selector)) return true;
    }
    return false;
  };
  const describe = (element, locator, visible, rect) => {
    const tag = element.localName;
    const type = tag === 'input' ? element.type : '';
    const disabled = element.matches(':disabled') || inherited(element, '[aria-disabled="true"]');
    const inert = inherited(element, '[inert]');
    const readonly = !!element.readOnly || element.getAttribute('aria-readonly') === 'true';
    const textual = tag === 'textarea' || element.isContentEditable ||
      (tag === 'input' && ['text', 'search', 'email', 'url', 'tel', 'password', 'number'].includes(type));
    const editable = !!(textual && !disabled && !inert && !readonly && visible);
    const root = element.getRootNode();
    const labelled = (element.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => root.getElementById?.(id)?.textContent || '').join(' ');
    const name = clean(labelled) || clean(element.getAttribute('aria-label')) ||
      clean([...(element.labels || [])].map(label => label.textContent).join(' ')) ||
      clean(element.getAttribute('placeholder')) || clean(element.getAttribute('title')) ||
      (tag === 'input' ? '' : clean(element.textContent));
    let tool = null;
    let reason;
    if (disabled || inert || readonly) reason = disabled ? 'disabled' : inert ? 'inert' : 'readonly';
    else if (tag === 'input' && type === 'file') {
      if (locator.shadow) reason = 'file_input_shadow_unsupported';
      else { tool = 'upload_files'; reason = 'file_input'; }
    }
    else if (!visible) reason = 'not_visible';
    else if (tag === 'canvas' || rect.width <= 0 || rect.height <= 0) {
      tool = 'capture_page_screenshot'; reason = 'verify_coordinate_target';
    } else if (editable) { tool = 'page_type'; reason = 'editable_text'; }
    else if (tag === 'select') reason = 'select_existing_option';
    else { tool = 'page_click'; reason = 'dom_target'; }
    return {
      locator, tag, type, name, role: element.getAttribute('role') || '',
      visible, editable, disabled, readonly, inert,
      rect: {x: rect.left, y: rect.top, width: rect.width, height: rect.height},
      hit_test: 'not_checked',
      recommended_tool: tool, reason,
    };
  };
  const walk = (root, shadow) => {
    for (const element of root.querySelectorAll('*')) {
      if (++visited > 10000) { truncated = true; return; }
      const candidate = element.matches(
        'input,textarea,select,button,a[href],[contenteditable],[role],[tabindex],[onclick],canvas,iframe,frame',
      );
      if (candidate) {
        const rendered = element.checkVisibility({opacityProperty: true, visibilityProperty: true});
        const file = element.localName === 'input' && element.type === 'file';
        if (rendered || file) {
          if (targets.length + frames.length >= limit) { truncated = true; return; }
          const css = selectorFor(element, root);
          if (css) {
            const locator = {css, ...(shadow.length ? {shadow: [...shadow]} : {})};
            const rect = element.getBoundingClientRect();
            if (['iframe', 'frame'].includes(element.localName)) {
              frames.push({locator, frame: [locator], name: clean(element.title || element.name),
                url: element.src || '', inspected: false});
            } else targets.push(describe(element, locator, rendered, rect));
          }
        }
      }
      if (element.shadowRoot) {
        const host = selectorFor(element, root);
        if (host) walk(element.shadowRoot, [...shadow, host]);
        if (truncated) return;
      }
    }
  };
  if (limit > 0 && document.body) walk(document, []);
  return {
    status: limit === 0 ? 'disabled' : 'success',
    url: location.href, title: document.title, ready_state: document.readyState,
    targets, frames, truncated, limit,
    viewport: {width: innerWidth, height: innerHeight, device_pixel_ratio: devicePixelRatio},
    coordinate_space: 'viewport_css',
  };
}

pageTargets;
pageOutline;
