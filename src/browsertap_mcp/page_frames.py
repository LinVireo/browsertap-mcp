"""Payloads for one attached, identity-bound operation through an iframe path."""

from __future__ import annotations

from typing import Any

from .page_input import (
    InputValidationError,
    normalize_locator,
    structured_locator_script,
)

# Runs on a retained iframe element in its own parent document. Browser origin
# policy never needs to be weakened: CDP resolves the child frame separately.
_FRAME_GUARD = r"""function(mode, x, y) {
  const frame = this;
  if (!frame.isConnected || frame.ownerDocument !== document)
    return {found:false, status:'stale_frame', stage:'frame'};
  if (!/^(IFRAME|FRAME)$/.test(frame.tagName))
    return {found:false, status:'not_a_frame', stage:'frame'};
  if (mode === 'identity') return {found:true};
  if (mode === 'focus') {
    let active = document.activeElement;
    while (active && active.shadowRoot && active.shadowRoot.activeElement)
      active = active.shadowRoot.activeElement;
    const focused = active === frame && document.hasFocus();
    return {found:focused, status:focused ? 'found' : 'focus_failed', stage:'frame'};
  }
  const identity = value => {
    if (!value || value === 'none') return true;
    const match = value.match(/^matrix(3d)?\(([^)]+)\)$/);
    if (!match) return false;
    const numbers = match[2].split(',').map(Number);
    const expected = match[1] ? [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1] : [1,0,0,1,0,0];
    return numbers.length === expected.length && numbers.every((n, i) => Math.abs(n - expected[i]) < 1e-9);
  };
  for (let node = frame; node; node = node.parentElement || node.getRootNode().host) {
    const style = getComputedStyle(node);
    if (!identity(style.transform) || (style.perspective && style.perspective !== 'none') ||
        (style.scale && style.scale !== 'none' && !/^1(?:\s+1){0,2}$/.test(style.scale)) ||
        (style.rotate && style.rotate !== 'none' && style.rotate !== '0deg') ||
        (style.translate && style.translate !== 'none' && !/^0px(?:\s+0px){0,2}$/.test(style.translate)) ||
        (style.zoom && style.zoom !== 'normal' && Number(style.zoom) !== 1))
      return {found:false, status:'unsupported_frame_transform', stage:'frame', frameTransform:style.transform || 'individual transform'};
  }
  const style = getComputedStyle(frame);
  const rect = frame.getBoundingClientRect();
  const paddingX = parseFloat(style.paddingLeft) || 0;
  const paddingY = parseFloat(style.paddingTop) || 0;
  const width = frame.clientWidth - paddingX - (parseFloat(style.paddingRight) || 0);
  const height = frame.clientHeight - paddingY - (parseFloat(style.paddingBottom) || 0);
  if (x < 0 || y < 0 || x >= width || y >= height)
    return {found:false, status:'outside_viewport', stage:'frame'};
  const px = rect.left + frame.clientLeft + paddingX + x;
  const py = rect.top + frame.clientTop + paddingY + y;
  if (px < 0 || py < 0 || px >= innerWidth || py >= innerHeight)
    return {found:false, status:'outside_viewport', stage:'frame'};
  if (mode === 'point') return {found:true, x:px, y:py};
  let hit = document.elementFromPoint(px, py);
  while (hit && hit.shadowRoot && hit.shadowRoot.elementFromPoint) {
    const inner = hit.shadowRoot.elementFromPoint(px, py);
    if (!inner || inner === hit) break;
    hit = inner;
  }
  if (hit !== frame) return {found:false, status:'obscured', stage:'frame',
    occludedBy:hit ? {tagName:hit.tagName, id:hit.id || ''} : null};
  return {found:true, x:px, y:py, hitVerified:true};
}"""


def frame_locator_payload(
    locator: dict[str, Any],
    *,
    action: str,
    offset_x: float = 0,
    offset_y: float = 0,
    center_x: bool = False,
    center_y: bool = False,
    clear: bool = False,
    gone: bool = False,
) -> dict[str, Any]:
    """Keep locator semantics shared with top-document tools, not a second DSL."""
    normalized = normalize_locator(locator)
    if not isinstance(normalized, dict) or not normalized.get("frame"):
        raise InputValidationError("frame locator requires a non-empty frame path")
    if action not in {"query", "click", "type"}:
        raise InputValidationError("frame action must be query, click, or type")
    point = "x" in normalized
    if point and action != "click":
        raise InputValidationError("frame-relative point locators are only valid for page_click")
    leaf = {key: value for key, value in normalized.items() if key != "frame"}
    def script(*, return_element: bool = False, bound_element: bool = False) -> str:
        return structured_locator_script(
            leaf, purpose=action, frame_context=True,
            offset_x=offset_x, offset_y=offset_y, center_x=center_x, center_y=center_y,
            select_all=clear, verify_hit=action == "click" and not point,
            return_element=return_element, bound_element=bound_element,
        )
    return {
        "cmd": "frame_locator",
        "action": action,
        "gone": gone,
        "point": {"x": leaf["x"], "y": leaf["y"]} if point else None,
        "frameSelectors": [
            structured_locator_script(spec, return_element=True)
            for spec in normalized["frame"]
        ],
        "nodeSelector": None if point else script(return_element=True),
        "inspect": None if point else script(bound_element=True),
        "observe": None if point else structured_locator_script(
            leaf, purpose="query", bound_element=True, frame_context=True,
        ),
        "frameGuard": _FRAME_GUARD,
        "centerX": center_x,
        "centerY": center_y,
    }
