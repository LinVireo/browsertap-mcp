// Find repeated-item containers, so `get_html` can collapse them.
//
// This replaces a scored search over every element on the page (a weighted
// child/grandchild count, a candidate cap and a minimum-children floor, all
// tuned constants). Repetition is a structural fact, not a scored guess: group
// an element's children by their own tag+class signature and the repeat is
// whichever signature occurs many times. No weights, and one pass.
//
// Contract, unchanged from what `simphtml.py` consumes: an array of objects
// carrying at least `selector` (a CSS selector matching the repeated items) and
// `itemCount`. `simphtml` re-queries the selector itself and independently
// re-checks item length, so a generous answer here costs nothing.
//
// The file must end with a bare `listGroups;` -- never a call.

function listGroups(startElement = null) {
  const MIN_ITEMS = 5;
  const MAX_GROUPS = 20;

  const saved = console.log;
  console.log = () => {};
  try {
    const root = startElement || document.body;
    if (!root) return [];

    // Identity of a *kind* of child, not of one child: tag plus the class list,
    // which is what makes list items look alike in every framework that renders
    // one template per record.
    const signature = (el) => {
      const cls = (el.getAttribute('class') || '')
        .trim().split(/\s+/).filter(Boolean).sort().join('.');
      return cls ? el.tagName + '.' + cls : el.tagName;
    };

    // Escape a class token for use in a selector. A generated class can contain
    // `:` or `/` (Tailwind) and `simphtml` passes this straight to
    // `soup.select`, so an unescaped token there is a dropped group.
    const esc = (token) => (window.CSS && CSS.escape ? CSS.escape(token) : token);

    const selectorFor = (parent, sample) => {
      const parts = [];
      if (parent.id) parts.push('#' + esc(parent.id));
      else if (parent !== document.body) parts.push(parent.tagName.toLowerCase());
      const cls = (sample.getAttribute('class') || '')
        .trim().split(/\s+/).filter(Boolean);
      let child = sample.tagName.toLowerCase();
      for (const token of cls) child += '.' + esc(token);
      parts.push(child);
      return parts.join(' > ');
    };

    const groups = [];
    // A container is any element with enough children; the repetition test then
    // decides whether it actually holds a list. Walking every element is what
    // the old code did too, but nothing here is scored.
    for (const parent of root.querySelectorAll('*')) {
      const children = parent.children;
      if (children.length < MIN_ITEMS) continue;
      if (parent.closest('svg')) continue;

      const bySignature = new Map();
      for (const child of children) {
        const key = signature(child);
        const bucket = bySignature.get(key);
        if (bucket) bucket.push(child);
        else bySignature.set(key, [child]);
      }

      for (const [key, items] of bySignature) {
        if (items.length < MIN_ITEMS) continue;
        // A row of icons is repetition too, but collapsing it saves nothing.
        // Measure instead of guessing: total markup the group occupies.
        const chars = items.reduce((sum, el) => sum + el.outerHTML.length, 0);
        groups.push({
          selector: selectorFor(parent, items[0]),
          itemCount: items.length,
          signature: key,
          chars,
          containerTag: parent.tagName,
          containerId: parent.id || '',
          containerClass: parent.getAttribute('class') || '',
          firstItemPreview: items[0].outerHTML.slice(0, 200),
        });
      }
    }

    // Biggest payload first: that is the one whose collapse saves the most, and
    // `simphtml` applies them in order.
    groups.sort((a, b) => b.chars - a.chars);

    // Drop a group whose selector also matches a group already returned -- an
    // ancestor and its descendant list would otherwise both be collapsed.
    const out = [];
    const seen = new Set();
    for (const group of groups) {
      if (seen.has(group.selector)) continue;
      seen.add(group.selector);
      out.push(group);
      if (out.length >= MAX_GROUPS) break;
    }
    return out;
  } finally {
    console.log = saved;
  }
}

listGroups;
