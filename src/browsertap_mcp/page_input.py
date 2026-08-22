"""Pure, deterministic CDP payload builders for page-level input."""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Any


class InputValidationError(ValueError):
    """Raised when a page-input request cannot be represented safely."""


_MODIFIER_ALIASES = {"alt": "alt", "ctrl": "control", "control": "control", "meta": "meta", "cmd": "meta", "shift": "shift"}
_MODIFIERS = {"alt": 1, "control": 2, "meta": 4, "shift": 8}
_MODIFIER_KEYS = {
    "alt": ("Alt", "AltLeft", 18),
    "control": ("Control", "ControlLeft", 17),
    "meta": ("Meta", "MetaLeft", 91),
    "shift": ("Shift", "ShiftLeft", 16),
}
_NAMED_KEYS = {
    "enter": ("Enter", "Enter", 13),
    "tab": ("Tab", "Tab", 9),
    "escape": ("Escape", "Escape", 27),
    "esc": ("Escape", "Escape", 27),
    "backspace": ("Backspace", "Backspace", 8),
    "arrowup": ("ArrowUp", "ArrowUp", 38),
    "arrowdown": ("ArrowDown", "ArrowDown", 40),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37),
    "arrowright": ("ArrowRight", "ArrowRight", 39),
    "home": ("Home", "Home", 36),
    "end": ("End", "End", 35),
    "pageup": ("PageUp", "PageUp", 33),
    "pagedown": ("PageDown", "PageDown", 34),
    "delete": ("Delete", "Delete", 46),
}
_BUTTONS = {"left", "middle", "right"}
_BUTTON_BITS = {"left": 1, "right": 2, "middle": 4}
_LOCATOR_KEYS = frozenset({"css", "role", "name", "text", "exact", "label", "frame", "shadow"})
_LOCATOR_PRIMARY_KEYS = ("css", "role", "text", "label")


def _command(method: str, **params: Any) -> dict[str, Any]:
    return {"cmd": "cdp", "method": method, "params": params}


#: Browser-side proof that the point about to be clicked belongs to the element
#: that was resolved. Without it a resolved rect is only evidence that the
#: element exists somewhere in the layout: a cookie banner, a modal backdrop or a
#: sticky header can sit on top of it, and an element below the fold resolves to
#: a point outside the viewport. In both cases ``Input.dispatchMouseEvent``
#: reports success, the click lands on whatever is really there, and nothing
#: anywhere reports a problem -- the same failure shape as the physical-input
#: tools in AGENTS.md section 4, one layer down.
#:
#: ``document.elementFromPoint`` is the browser's own answer to "who is on top
#: here", already used for z-index ground truth in ``simphtml``. Coordinates are
#: viewport-local, so the test runs in the element's own document and the caller
#: adds any frame offset afterwards.
_HIT_TEST_JS = r"""
  const hitTest = (el, px, py) => {
    const doc = el.ownerDocument;
    const view = (doc && doc.defaultView) || window;
    const width = view.innerWidth || 0;
    const height = view.innerHeight || 0;
    if (px < 0 || py < 0 || px >= width || py >= height) return {ok:false, status:'outside_viewport'};
    let hit = null;
    try { hit = doc.elementFromPoint(px, py); }
    catch (_) { return {ok:true, note:'hit_test_unavailable'}; }
    if (!hit) return {ok:false, status:'outside_viewport'};
    // A label whose text node is on top, or an icon inside a button, is a hit.
    if (hit === el || el.contains(hit) || hit.contains(el)) return {ok:true};
    // elementFromPoint stops at a shadow host, so climb el's chain of hosts.
    let node = el;
    for (let depth = 0; depth < 32 && node; depth += 1) {
      const root = node.getRootNode && node.getRootNode();
      const host = root && root.host;
      if (!host) break;
      if (host === hit || hit.contains(host)) return {ok:true};
      node = host;
    }
    const classes = (typeof hit.className === 'string' ? hit.className : '').trim();
    const label = [
      (hit.tagName || '?').toLowerCase(),
      hit.id ? '#' + hit.id : '',
      classes ? '.' + classes.split(/\s+/).slice(0, 3).join('.') : ''
    ].join('');
    return {ok:false, status:'obscured', occludedBy:label};
  };
"""


def _number(value: Any, name: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InputValidationError(f"{name} must be a finite number")
    return value


def _mouse_event(event_type: str, x: float | int, y: float | int, **extra: Any) -> dict[str, Any]:
    return _command("Input.dispatchMouseEvent", type=event_type, x=x, y=y, **extra)


def _button(value: Any) -> str:
    if not isinstance(value, str) or value not in _BUTTONS:
        raise InputValidationError("button must be left, middle, or right")
    return value


def click_commands(x: float, y: float, *, button: str = "left", clicks: int = 1) -> list[dict[str, Any]]:
    """Build a move, press, release CDP click sequence."""
    x = _number(x, "x")
    y = _number(y, "y")
    button = _button(button)
    if isinstance(clicks, bool) or not isinstance(clicks, int) or clicks < 1:
        raise InputValidationError("clicks must be a positive integer")
    shared = {"button": button, "clickCount": clicks}
    return [
        _mouse_event("mouseMoved", x, y),
        _mouse_event("mousePressed", x, y, buttons=_BUTTON_BITS[button], **shared),
        _mouse_event("mouseReleased", x, y, buttons=0, **shared),
    ]


def drag_commands(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    *,
    duration: float = 0.3,
    button: str = "left",
) -> list[dict[str, Any]]:
    """Build a bounded, deterministic press-and-drag mouse sequence."""
    start_x = _number(start_x, "start_x")
    start_y = _number(start_y, "start_y")
    end_x = _number(end_x, "end_x")
    end_y = _number(end_y, "end_y")
    duration = _number(duration, "duration")
    if not 0 <= duration <= 10:
        raise InputValidationError("duration must be between 0 and 10 seconds")
    button = _button(button)

    # At most twenty in-flight points keeps even a ten-second drag compact.
    steps = min(20, max(1, math.ceil(duration * 20)))
    commands = [
        _mouse_event("mouseMoved", start_x, start_y),
        _mouse_event(
            "mousePressed",
            start_x,
            start_y,
            button=button,
            buttons=_BUTTON_BITS[button],
            clickCount=1,
        ),
    ]
    for step in range(1, steps + 1):
        fraction = step / steps
        commands.append(
            _mouse_event(
                "mouseMoved",
                start_x + (end_x - start_x) * fraction,
                start_y + (end_y - start_y) * fraction,
                button=button,
                buttons=_BUTTON_BITS[button],
            )
        )
    commands.append(
        _mouse_event(
            "mouseReleased",
            end_x,
            end_y,
            button=button,
            buttons=0,
            clickCount=1,
        )
    )
    # Chromium can retain the drag's pressed-button state when the debugger is
    # detached immediately after mouseReleased. A zero-button move commits the
    # released state before the batch lease is torn down.
    commands.append(_mouse_event("mouseMoved", end_x, end_y, buttons=0))
    return commands


def _key_details(key: str) -> tuple[str, str, int]:
    if not isinstance(key, str) or not key:
        raise InputValidationError("key must be a supported key name or one printable character")
    named = _NAMED_KEYS.get(key.lower())
    if named:
        return named
    if len(key) == 1 and key.isprintable():
        if key.isalpha():
            return key, f"Key{key.upper()}", ord(key.upper())
        if key.isdigit():
            return key, f"Digit{key}", ord(key)
        return key, "", ord(key)
    raise InputValidationError(f"unsupported key: {key}")


def press_commands(chord: str) -> list[dict[str, Any]]:
    """Build CDP keyboard events for a comma-delimited key chord."""
    if not isinstance(chord, str):
        raise InputValidationError("chord must be a comma-delimited string")
    parts = [part.strip() for part in chord.split(",")]
    if not parts or any(not part for part in parts):
        raise InputValidationError("chord contains an empty key")
    if len(parts) > 1:
        raw_modifier_names = [part.lower() for part in parts[:-1]]
        if any(name not in _MODIFIER_ALIASES for name in raw_modifier_names):
            raise InputValidationError("only modifiers may precede the final key")
        modifier_names = [_MODIFIER_ALIASES[name] for name in raw_modifier_names]
        if len(set(modifier_names)) != len(modifier_names):
            raise InputValidationError("a modifier may appear only once")
    else:
        modifier_names = []
    key, code, key_code = _key_details(parts[-1])
    if parts[-1].lower() in _MODIFIER_ALIASES:
        raise InputValidationError("chord must end with a non-modifier key")

    modifiers = 0
    commands: list[dict[str, Any]] = []
    for modifier_name in modifier_names:
        modifier_key, modifier_code, modifier_code_value = _MODIFIER_KEYS[modifier_name]
        modifiers |= _MODIFIERS[modifier_name]
        commands.append(
            _command(
                "Input.dispatchKeyEvent",
                type="rawKeyDown",
                key=modifier_key,
                code=modifier_code,
                windowsVirtualKeyCode=modifier_code_value,
                nativeVirtualKeyCode=modifier_code_value,
                modifiers=modifiers,
            )
        )

    key_params = {
        "key": key,
        "code": code,
        "windowsVirtualKeyCode": key_code,
        "nativeVirtualKeyCode": key_code,
        "modifiers": modifiers,
    }
    commands.extend(
        [
            _command(
                "Input.dispatchKeyEvent",
                type="rawKeyDown" if modifiers else "keyDown",
                **key_params,
            ),
            _command("Input.dispatchKeyEvent", type="keyUp", **key_params),
        ]
    )
    for modifier_name in reversed(modifier_names):
        modifier_key, modifier_code, modifier_code_value = _MODIFIER_KEYS[modifier_name]
        commands.append(
            _command(
                "Input.dispatchKeyEvent",
                type="keyUp",
                key=modifier_key,
                code=modifier_code,
                windowsVirtualKeyCode=modifier_code_value,
                nativeVirtualKeyCode=modifier_code_value,
                modifiers=modifiers,
            )
        )
        modifiers &= ~_MODIFIERS[modifier_name]
    return commands


def type_commands(
    selector: str,
    text: str,
    *,
    select_all: bool = False,
    submit_key: str | None = None,
    submit_delay_ms: int = 0,
) -> list[dict[str, Any]]:
    """Focus a matching element, insert text, and optionally submit a key.

    An empty selector means "use the focused editor". Xterm.js keeps its real
    input sink in a hidden ``.xterm-helper-textarea``; a background tab often
    leaves ``document.body`` focused, so plain ``Input.insertText`` otherwise
    disappears. Resolve xterm containers/descendants to that helper before the
    trusted CDP input is dispatched.
    """
    if not isinstance(text, str):
        raise InputValidationError("text must be a string")
    if (
        isinstance(submit_delay_ms, bool)
        or not isinstance(submit_delay_ms, int)
        or submit_delay_ms < 0
    ):
        raise InputValidationError("submit_delay_ms must be a non-negative integer")
    expression = type_target_script(selector, select_all=select_all)
    commands = [
        _command("Runtime.evaluate", expression=expression, returnByValue=True),
        _command("Input.insertText", text=text),
    ]
    if submit_key is not None:
        if submit_delay_ms:
            commands.append(
                _command(
                    "Runtime.evaluate",
                    expression=(
                        "new Promise(resolve => setTimeout(resolve, "
                        f"{submit_delay_ms}))"
                    ),
                    awaitPromise=True,
                    returnByValue=True,
                )
            )
        commands.extend(press_commands(submit_key))
    return commands


def type_target_script(selector: str, *, select_all: bool = False) -> str:
    """Resolve and focus the exact sink used by :func:`type_commands`.

    The server runs this resolver first and only sends trusted CDP text/key
    input after ``found:true``. That split is deliberate: a missing selector
    must never let ``Input.insertText`` fall through to some previously focused
    field in the same tab.
    """
    if not isinstance(selector, str):
        raise InputValidationError("selector must be a string")
    if not isinstance(select_all, bool):
        raise InputValidationError("select_all must be a boolean")
    selector_json = json.dumps(selector)
    select_all_json = json.dumps(select_all)
    return f"""(() => {{
  const selector = {selector_json};
  const selectAll = {select_all_json};
  let el = selector ? document.querySelector(selector) : document.activeElement;
  if (selector && !el) return {{found:false, targetKind:'missing'}};
  const helpers = [...document.querySelectorAll('.xterm-helper-textarea')];
  let xtermRoot = null;
  if (el && el.matches && el.matches('.xterm')) xtermRoot = el;
  else if (el && el.closest) xtermRoot = el.closest('.xterm');
  let helper = el && el.matches && el.matches('.xterm-helper-textarea') ? el : null;
  if (!helper && xtermRoot) helper = xtermRoot.querySelector('.xterm-helper-textarea');
  const textCapable = el && (
    /^(INPUT|TEXTAREA)$/.test(el.tagName || '') || el.isContentEditable
  );
  if (!selector && !textCapable && helpers.length === 1) helper = helpers[0];
  if (helper) el = helper;
  if (!el || el === document.body || el === document.documentElement) {{
    return {{found:false, targetKind:'missing'}};
  }}
  try {{ el.focus({{preventScroll:true}}); }} catch (_) {{ el.focus(); }}
  if (selectAll) {{
    if (typeof el.select === 'function') el.select();
    else if (el.isContentEditable) {{
      const range = document.createRange();
      range.selectNodeContents(el);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }}
  }}
  return {{
    found:true,
    targetKind: helper ? 'xterm' : 'element',
    tagName: el.tagName || '',
  }};
}})()"""


def normalize_locator(locator: str | dict[str, Any], *, nested: bool = False) -> str | dict[str, Any]:
    """Validate a public locator while preserving legacy CSS strings verbatim."""
    if isinstance(locator, str):
        if not locator:
            raise InputValidationError("selector must be a non-empty string")
        return locator
    if not isinstance(locator, dict):
        raise InputValidationError("selector must be a CSS string or locator object")
    unknown = sorted(set(locator) - _LOCATOR_KEYS)
    if unknown:
        raise InputValidationError(f"unknown locator field(s): {', '.join(unknown)}")
    primary = [key for key in _LOCATOR_PRIMARY_KEYS if locator.get(key) not in (None, "")]
    if len(primary) != 1:
        raise InputValidationError(
            "locator requires exactly one of css, role, text, or label"
        )
    if locator.get("name") not in (None, "") and primary[0] != "role":
        raise InputValidationError("locator name is only valid with role")
    if "exact" in locator and not isinstance(locator["exact"], bool):
        raise InputValidationError("locator exact must be a boolean")
    if primary[0] not in {"role", "text"} and "exact" in locator:
        raise InputValidationError("locator exact is only valid with role/name or text")
    normalized: dict[str, Any] = {primary[0]: str(locator[primary[0]])}
    if not normalized[primary[0]]:
        raise InputValidationError(f"locator {primary[0]} must be non-empty")
    if primary[0] == "role" and locator.get("name") not in (None, ""):
        normalized["name"] = str(locator["name"])
    if "exact" in locator:
        normalized["exact"] = locator["exact"]

    if "frame" in locator:
        if nested:
            raise InputValidationError("nested frame locators are not supported")
        frames = locator["frame"] if isinstance(locator["frame"], list) else [locator["frame"]]
        if not frames:
            raise InputValidationError("locator frame must not be empty")
        normalized_frames = []
        for frame in frames:
            normalized_frame = normalize_locator(frame, nested=True)
            normalized_frames.append(
                {"css": normalized_frame}
                if isinstance(normalized_frame, str)
                else normalized_frame
            )
        normalized["frame"] = normalized_frames
    if "shadow" in locator:
        shadows = locator["shadow"]
        if isinstance(shadows, str):
            shadows = [shadows]
        if not isinstance(shadows, list) or not shadows or any(
            not isinstance(item, str) or not item for item in shadows
        ):
            raise InputValidationError("locator shadow must be a non-empty CSS string or list")
        normalized["shadow"] = list(shadows)
    return normalized


def structured_locator_script(
    locator: dict[str, Any],
    *,
    purpose: str = "query",
    offset_x: float = 0,
    offset_y: float = 0,
    select_all: bool = False,
    verify_hit: bool = False,
    center_x: bool = False,
    center_y: bool = False,
) -> str:
    """Build the strict browser-side resolver for structured locators."""
    locator = normalize_locator(locator)  # type: ignore[assignment]
    if not isinstance(locator, dict):  # pragma: no cover - guarded by the signature
        raise InputValidationError("structured locator must be an object")
    if purpose not in {"query", "click", "type"}:
        raise InputValidationError("locator purpose must be query, click, or type")
    offset_x = _number(offset_x, "offset_x")
    offset_y = _number(offset_y, "offset_y")
    for name, flag in (
        ("select_all", select_all),
        ("verify_hit", verify_hit),
        ("center_x", center_x),
        ("center_y", center_y),
    ):
        if not isinstance(flag, bool):
            raise InputValidationError(f"{name} must be a boolean")
    return f"""(() => {{
  const locator = {json.dumps(locator, ensure_ascii=False)};
  const purpose = {json.dumps(purpose)};
  const offsetX = {json.dumps(offset_x)};
  const offsetY = {json.dumps(offset_y)};
  const selectAll = {json.dumps(select_all)};
  const verifyHit = {json.dumps(verify_hit)};
  const centerX = {json.dumps(center_x)};
  const centerY = {json.dumps(center_y)};
  const framed = {json.dumps(bool(locator.get("frame")))};{_HIT_TEST_JS}
  const clean = value => String(value == null ? '' : value).replace(/\\s+/g, ' ').trim();
  const same = (actual, expected, exact) => exact ? clean(actual) === clean(expected) : clean(actual).includes(clean(expected));
  const implicitRole = el => {{
    const tag = (el.tagName || '').toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'img') return 'img';
    if (tag === 'option') return 'option';
    if (tag === 'input') {{
      const type = (el.type || 'text').toLowerCase();
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'button' || type === 'submit' || type === 'reset') return 'button';
      if (!['hidden', 'file', 'color', 'range'].includes(type)) return 'textbox';
    }}
    return '';
  }};
  const accessibleName = el => {{
    const labelled = clean(el.getAttribute && el.getAttribute('aria-labelledby'));
    if (labelled) {{
      const text = labelled.split(/\\s+/).map(id => el.ownerDocument.getElementById(id)).filter(Boolean)
        .map(node => clean(node.textContent)).join(' ');
      if (text) return text;
    }}
    const aria = clean(el.getAttribute && el.getAttribute('aria-label'));
    if (aria) return aria;
    if (el.labels && el.labels.length) return clean([...el.labels].map(label => label.textContent).join(' '));
    return clean((el.getAttribute && (el.getAttribute('alt') || el.getAttribute('title') || el.getAttribute('value'))) || el.textContent);
  }};
  const allElements = root => [...root.querySelectorAll('*')];
  const find = (root, spec) => {{
    let matches = [];
    if (spec.css) {{
      try {{ matches = [...root.querySelectorAll(spec.css)]; }}
      catch (error) {{ return {{error:'invalid_selector', message:String(error && error.message || error)}}; }}
    }} else if (spec.role) {{
      matches = allElements(root).filter(el => clean(el.getAttribute('role') || implicitRole(el)).toLowerCase() === clean(spec.role).toLowerCase());
      if (spec.name) matches = matches.filter(el => same(accessibleName(el), spec.name, !!spec.exact));
    }} else if (spec.text) {{
      matches = allElements(root).filter(el => same(el.textContent, spec.text, !!spec.exact));
      matches = matches.filter(el => ![...el.children].some(child => same(child.textContent, spec.text, !!spec.exact)));
    }} else if (spec.label) {{
      const labels = allElements(root).filter(el => (el.tagName || '').toLowerCase() === 'label' && same(el.textContent, spec.label, true));
      matches = labels.map(label => label.control || (label.htmlFor && label.ownerDocument.getElementById(label.htmlFor)) || label.querySelector('input,textarea,select,button')).filter(Boolean);
    }}
    return {{matches}};
  }};
  let root = document;
  let frameOffsetX = 0;
  let frameOffsetY = 0;
  for (const frameSpec of (locator.frame || [])) {{
    const found = find(root, frameSpec);
    if (found.error) return {{found:false, status:found.error, error:found.message}};
    if (found.matches.length === 0) return {{found:false, status:'not_found', stage:'frame'}};
    if (found.matches.length > 1) return {{found:false, status:'ambiguous', matches:found.matches.length, stage:'frame'}};
    const frame = found.matches[0];
    const frameRect = frame.getBoundingClientRect();
    let doc = null;
    try {{ doc = frame.contentDocument; }} catch (_) {{}}
    if (!doc) return {{found:false, status:'cross_origin_frame', stage:'frame'}};
    frameOffsetX += frameRect.left + (frame.clientLeft || 0);
    frameOffsetY += frameRect.top + (frame.clientTop || 0);
    root = doc;
  }}
  for (const hostSelector of (locator.shadow || [])) {{
    let hosts = [];
    try {{ hosts = [...root.querySelectorAll(hostSelector)]; }}
    catch (error) {{ return {{found:false, status:'invalid_selector', error:String(error && error.message || error), stage:'shadow'}}; }}
    if (hosts.length === 0) return {{found:false, status:'not_found', stage:'shadow'}};
    if (hosts.length > 1) return {{found:false, status:'ambiguous', matches:hosts.length, stage:'shadow'}};
    if (!hosts[0].shadowRoot) return {{found:false, status:'closed_shadow_root', stage:'shadow'}};
    root = hosts[0].shadowRoot;
  }}
  const found = find(root, locator);
  if (found.error) return {{found:false, status:found.error, error:found.message}};
  if (found.matches.length === 0) return {{found:false, status:'not_found'}};
  if (found.matches.length > 1) return {{found:false, status:'ambiguous', matches:found.matches.length}};
  let el = found.matches[0];
  if (purpose === 'type') {{
    let helper = el.matches && el.matches('.xterm-helper-textarea') ? el : null;
    const xtermRoot = el.matches && el.matches('.xterm') ? el : (el.closest && el.closest('.xterm'));
    if (!helper && xtermRoot) helper = xtermRoot.querySelector('.xterm-helper-textarea');
    if (helper) el = helper;
    const textCapable = /^(INPUT|TEXTAREA)$/.test(el.tagName || '') || el.isContentEditable || !!helper;
    const ariaDisabled = clean(el.getAttribute && el.getAttribute('aria-disabled')).toLowerCase() === 'true';
    if (!textCapable || el.disabled || el.readOnly || ariaDisabled) return {{found:false, status:'not_interactable', targetKind:'unusable'}};
    try {{ el.focus({{preventScroll:true}}); }} catch (_) {{ el.focus(); }}
    if (selectAll) {{
      if (typeof el.select === 'function') el.select();
      else if (el.isContentEditable) {{
        const range = el.ownerDocument.createRange(); range.selectNodeContents(el);
        const selection = el.ownerDocument.defaultView.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      }}
    }}
    return {{found:true, status:'found', targetKind:helper ? 'xterm' : 'element', tagName:el.tagName || ''}};
  }}
  let rect = el.getBoundingClientRect();
  const ariaDisabled = clean(el.getAttribute && el.getAttribute('aria-disabled')).toLowerCase() === 'true';
  if (purpose === 'click' && (rect.width <= 0 || rect.height <= 0 || el.disabled || ariaDisabled)) return {{found:false, status:'not_interactable'}};
  let hitVerified = false;
  let scrolledIntoView = false;
  if (purpose === 'click' && verifyHit) {{
    const pointX = () => rect.left + (centerX ? rect.width / 2 : offsetX);
    const pointY = () => rect.top + (centerY ? rect.height / 2 : offsetY);
    let probe = hitTest(el, pointX(), pointY());
    if (!probe.ok && probe.status === 'outside_viewport' && !framed) {{
      // Below the fold is the common case and scrolling is what a person would
      // do. Not inside a frame: scrolling there can move an ancestor frame too,
      // which would invalidate the frame offsets measured above.
      try {{ el.scrollIntoView({{block:'center', inline:'center'}}); }}
      catch (_) {{ try {{ el.scrollIntoView(); }} catch (_) {{}} }}
      scrolledIntoView = true;
      rect = el.getBoundingClientRect();
      probe = hitTest(el, pointX(), pointY());
    }}
    if (!probe.ok) return {{found:false, status:probe.status, occludedBy:probe.occludedBy || null, scrolledIntoView}};
    hitVerified = !probe.note;
  }}
  const challengeSelector = '.cf-turnstile, cf-turnstile, iframe[src*="challenges.cloudflare.com"], [src*="challenges.cloudflare.com"]';
  const elementSignal = [el.tagName, el.id, el.className, el.getAttribute && el.getAttribute('src'), el.getAttribute && el.getAttribute('name')].filter(Boolean).join(' ').toLowerCase();
  const pageSignal = [document.title, location.hostname, location.href].join(' ').toLowerCase();
  const elementIsChallenge = !!(el.matches && el.matches(challengeSelector)) || elementSignal.includes('cf-turnstile') || elementSignal.includes('challenges.cloudflare.com');
  const pageChallengeElement = document.querySelector(challengeSelector);
  const markerElement = elementIsChallenge ? el : pageChallengeElement;
  const challenge = elementIsChallenge || pageSignal.includes('challenges.cloudflare.com') || !!pageChallengeElement || /cloudflare.*challenge|challenge.*cloudflare|just a moment/.test(document.title.toLowerCase());
  const challengeMarker = challenge ? [location.origin, location.pathname, markerElement ? markerElement.tagName : 'page', markerElement ? markerElement.id : '', markerElement ? markerElement.className : '', markerElement && markerElement.getAttribute ? markerElement.getAttribute('src') || '' : '', markerElement && markerElement.getAttribute ? markerElement.getAttribute('data-sitekey') || '' : ''].join('|') : null;
  return {{found:true, status:'found', x:frameOffsetX + rect.left + offsetX, y:frameOffsetY + rect.top + offsetY, width:rect.width, height:rect.height, challengeMarker, hitVerified, scrolledIntoView}};
}})()"""


def locator_query_script(locator: str | dict[str, Any]) -> str:
    """Return a side-effect-free expression whose result describes one locator."""
    normalized = normalize_locator(locator)
    if isinstance(normalized, str):
        return resolve_selector_script(normalized)
    return structured_locator_script(normalized, purpose="query")


def resolve_selector_script(
    selector: str,
    offset_x: float = 0,
    offset_y: float = 0,
    *,
    require_interactable: bool = False,
    verify_hit: bool = False,
    center_x: bool = False,
    center_y: bool = False,
) -> str:
    """Return a browser-side selector resolver with deterministic JSON quoting."""
    if not isinstance(selector, str) or not selector:
        raise InputValidationError("selector must be a non-empty string")
    offset_x = _number(offset_x, "offset_x")
    offset_y = _number(offset_y, "offset_y")
    for name, flag in (
        ("require_interactable", require_interactable),
        ("verify_hit", verify_hit),
        ("center_x", center_x),
        ("center_y", center_y),
    ):
        if not isinstance(flag, bool):
            raise InputValidationError(f"{name} must be a boolean")
    return """(() => {
  const selector = %s;
  const offsetX = %s;
  const offsetY = %s;
  const requireInteractable = %s;
  const verifyHit = %s;
  const centerX = %s;
  const centerY = %s;%s
  const element = document.querySelector(selector);
  if (!element) return {found:false};
  let rect = element.getBoundingClientRect();
  const ariaDisabled = String(element.getAttribute('aria-disabled') || '').trim().toLowerCase() === 'true';
  if (requireInteractable && (rect.width <= 0 || rect.height <= 0 || element.disabled || ariaDisabled)) {
    return {found:false, status:'not_interactable'};
  }
  let hitVerified = false;
  let scrolledIntoView = false;
  if (verifyHit) {
    const pointX = () => rect.left + (centerX ? rect.width / 2 : offsetX);
    const pointY = () => rect.top + (centerY ? rect.height / 2 : offsetY);
    let probe = hitTest(element, pointX(), pointY());
    if (!probe.ok && probe.status === 'outside_viewport') {
      try { element.scrollIntoView({block:'center', inline:'center'}); }
      catch (_) { try { element.scrollIntoView(); } catch (_) {} }
      scrolledIntoView = true;
      rect = element.getBoundingClientRect();
      probe = hitTest(element, pointX(), pointY());
    }
    if (!probe.ok) return {found:false, status:probe.status, occludedBy:probe.occludedBy || null, scrolledIntoView:scrolledIntoView};
    hitVerified = !probe.note;
  }
  const challengeSelector = '.cf-turnstile, cf-turnstile, iframe[src*="challenges.cloudflare.com"], [src*="challenges.cloudflare.com"]';
  const elementSignal = [element.tagName, element.id, element.className, element.getAttribute('src'), element.getAttribute('name')]
    .filter(Boolean).join(' ').toLowerCase();
  const pageSignal = [document.title, location.hostname, location.href].join(' ').toLowerCase();
  const elementIsChallenge = element.matches(challengeSelector) ||
    elementSignal.includes('cf-turnstile') || elementSignal.includes('challenges.cloudflare.com');
  const pageChallengeElement = document.querySelector(challengeSelector);
  const markerElement = elementIsChallenge ? element : pageChallengeElement;
  const challengeTitle = /cloudflare.*challenge|challenge.*cloudflare|just a moment/.test(document.title.toLowerCase());
  const challenge = elementIsChallenge || pageSignal.includes('challenges.cloudflare.com') ||
    Boolean(pageChallengeElement) || challengeTitle;
  const challengeMarker = challenge
    ? [location.origin, location.pathname, markerElement ? markerElement.tagName : 'page',
      markerElement ? markerElement.id : '', markerElement ? markerElement.className : '',
      markerElement ? markerElement.getAttribute('src') || '' : '',
      markerElement ? markerElement.getAttribute('data-sitekey') || '' : ''].join('|')
    : null;
  return {
    found:true,
    x:rect.left + offsetX,
    y:rect.top + offsetY,
    width:rect.width,
    height:rect.height,
    challengeMarker:challengeMarker,
    hitVerified:hitVerified,
    scrolledIntoView:scrolledIntoView
  };
})()""" % (
        json.dumps(selector),
        json.dumps(offset_x),
        json.dumps(offset_y),
        json.dumps(require_interactable),
        json.dumps(verify_hit),
        json.dumps(center_x),
        json.dumps(center_y),
        _HIT_TEST_JS,
    )


@dataclass
class _ChallengeState:
    marker: str
    started_at: float
    count: int


class ChallengeAttemptTracker:
    """Track repeated challenge attempts per CDP session without browser state."""

    def __init__(self, max_attempts: int = 3, window_seconds: float = 120) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise InputValidationError("max_attempts must be a positive integer")
        window_seconds = _number(window_seconds, "window_seconds")
        if window_seconds <= 0:
            raise InputValidationError("window_seconds must be positive")
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._states: dict[str, _ChallengeState] = {}
        self._lock = threading.Lock()

    def record(self, session_id: str, marker: str, *, now: float | None = None) -> bool:
        """Record an attempt and return true once the identical marker stalls."""
        if not isinstance(session_id, str) or not session_id:
            raise InputValidationError("session_id must be a non-empty string")
        if not isinstance(marker, str) or not marker:
            raise InputValidationError("marker must be a non-empty string")
        current = time.monotonic() if now is None else _number(now, "now")
        with self._lock:
            state = self._states.get(session_id)
            if state is None or state.marker != marker or current - state.started_at >= self.window_seconds:
                state = _ChallengeState(marker=marker, started_at=current, count=0)
                self._states[session_id] = state
            state.count += 1
            return state.count >= self.max_attempts

    def clear(self, session_id: str) -> None:
        """Forget all challenge attempts for one session."""
        if not isinstance(session_id, str) or not session_id:
            raise InputValidationError("session_id must be a non-empty string")
        with self._lock:
            self._states.pop(session_id, None)
