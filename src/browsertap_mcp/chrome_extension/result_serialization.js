// Shared by page injection, the Python CDP fallback and raw manual CDP results.
// Keep this function self-contained: the worker injects its function source.
function smartProcessResult(result, depth = 0, seen = new WeakSet()) {
  const maxItems = 200;
  const maxDepth = 6;
  if (result === undefined || result === null) return null;
  if (typeof result === 'bigint' || typeof result === 'symbol') return String(result);
  if (typeof result === 'number') return Number.isFinite(result) ? result : null;
  if (typeof result === 'function') return '[Function: ' + (result.name || 'anonymous') + ']';
  if (typeof result !== 'object') return result;
  try {
    const kind = Object.prototype.toString.call(result).slice(8, -1);
    if (kind === 'Window') {
      let href = 'cross-origin';
      try { href = result.location.href; } catch (_) {}
      return '[Window: ' + href + ']';
    }
    if (kind === 'Error') return '[' + (result.name || 'Error') + ': ' + result.message + ']';
    if (typeof result.outerHTML === 'string') return result.outerHTML;
    if (typeof result.nodeType === 'number') {
      return '[' + kind + (result.nodeValue ? ': ' + result.nodeValue : '') + ']';
    }
    if (seen.has(result)) return '[Circular]';
    if (depth >= maxDepth) return '[' + kind + ': depth limit]';
    seen.add(result);
    try {
      // A hook can return another non-JSON value or its own receiver. Run it
      // through the same bounds and cycle detection instead of trusting it.
      if (typeof result.toJSON === 'function') {
        try { return smartProcessResult(result.toJSON(), depth + 1, seen); } catch (_) {}
      }
      const iterable = typeof result[Symbol.iterator] === 'function';
      const arrayLike = kind !== 'Object' && typeof result.length === 'number';
      if (iterable || arrayLike) {
        const out = [];
        const count = arrayLike ? result.length : result.size;
        const total = Number.isSafeInteger(count) && count >= 0 ? count : null;
        let more = false;
        if (iterable) {
          // One lookahead establishes truncation without draining a generator.
          // The break also invokes IteratorClose on an unfinished iterator.
          for (const value of result) {
            if (out.length === maxItems) { more = true; break; }
            out.push(smartProcessResult(value, depth + 1, seen));
          }
        } else {
          const length = Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0;
          for (let i = 0; i < Math.min(length, maxItems); i += 1) {
            out.push(smartProcessResult(result[i], depth + 1, seen));
          }
          more = length > maxItems;
        }
        if (more) {
          out.push(total !== null && total > maxItems
            ? '[' + (total - maxItems) + ' more of ' + total + ']'
            : '[more items; limit ' + maxItems + ']');
        }
        return out;
      }
      const out = Object.create(null);
      for (const key of Object.keys(result)) {
        try {
          out[key] = smartProcessResult(result[key], depth + 1, seen);
        } catch (error) {
          out[key] = '[unreadable: ' + (error?.message || String(error)) + ']';
        }
      }
      return out;
    } finally {
      seen.delete(result);
    }
  } catch (error) {
    return '[unreadable: ' + (error?.message || String(error)) + ']';
  }
}

smartProcessResult;
