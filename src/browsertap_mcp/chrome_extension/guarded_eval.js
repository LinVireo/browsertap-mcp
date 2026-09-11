// Prepare a direct eval without confusing user-thrown SyntaxError with a parse
// error. The returned source keeps eval completion values and strictness.
function prepareGuardedEval(code, receiver) {
  // Hashbang comments belong to Script grammar, but not FunctionBody grammar.
  // Removing this comment lets the compile-only probe accept the same body.
  const body = code.replace(/^#![^\r\n\u2028\u2029]*(?:\r\n?|[\n\u2028\u2029]|$)/, '');
  const FunctionConstructor = (function() {}).constructor;
  new FunctionConstructor(body);

  // The native parser recognizes the complete Directive Prologue, including
  // comments, escapes and ASI continuations. A named function with a strict
  // body cannot be called `eval`. This probe is never invoked; because the
  // unnamed body compiled above, its only extra restriction is this name.
  let strict = false;
  try {
    new FunctionConstructor('return function eval(){\n' + body + '\n}');
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    strict = true;
  }

  if (receiver === null ||
      (typeof receiver !== 'object' && typeof receiver !== 'function')) {
    throw new Error('direct eval requires an object receiver for execution tracking');
  }
  const base = '__btap_eval_' + Date.now() + '_' + Math.random().toString(36).slice(2);
  let key = base;
  for (let suffix = 1; key in receiver; suffix += 1) key = base + '_' + suffix;
  let started = false;
  const cleanup = () => { delete receiver[key]; };
  Object.defineProperty(receiver, key, {
    configurable: true,
    value: () => {
      started = true;
      cleanup();
    },
  });
  // `this` cannot be shadowed by a caller's var/let names. The temporary
  // property is gone before the first caller statement or property enumeration.
  const source = (strict ? '"use strict";\n' : '') +
    'this[' + JSON.stringify(key) + ']();\n' + body;
  return Object.freeze({ source, started: () => started, cleanup });
}

// AsyncFunction has no Script completion value. Add a return only when the
// native parser confirms one complete expression or a safe final expression
// line. Otherwise preserve the entire authored function body and its returns.
prepareGuardedEval.asyncBody = function prepareAsyncBody(code) {
  const body = code.replace(/^#![^\r\n\u2028\u2029]*(?:\r\n?|[\n\u2028\u2029]|$)/, '');
  const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
  new AsyncFunction(body);

  function skipTrivia(source) {
    // Only whitespace and comments are recognized here. All executable syntax
    // is still checked by the native parser before any candidate is used.
    return source.replace(/^(?:\s+|\/\/[^\r\n\u2028\u2029]*|\/\*[\s\S]*?\*\/)*/, '');
  }

  function expressionBody(source) {
    const first = skipTrivia(source);
    // These statement forms also parse inside parentheses, but changing them
    // to expressions would alter bindings or turn a labeled block into data.
    if (/^(?:function|class)(?![$\p{ID_Continue}\u200c\u200d])|^\{/u.test(first)) return null;
    if (/^async(?![$\p{ID_Continue}\u200c\u200d])/u.test(first) &&
        /^function(?![$\p{ID_Continue}\u200c\u200d])/u.test(skipTrivia(first.slice(5)))) return null;
    const candidates = [source, source.replace(/[;\s]+$/, '')];
    const terminator = source.lastIndexOf(';');
    if (terminator >= 0 && !skipTrivia(source.slice(terminator + 1))) {
      candidates.push(source.slice(0, terminator));
    }
    for (const candidate of candidates) {
      const returned = 'return (\n' + candidate + '\n);';
      try {
        new AsyncFunction(returned);
        return returned;
      } catch (error) {
        if (!(error instanceof SyntaxError)) throw error;
      }
    }
    return null;
  }

  const completeExpression = expressionBody(body);
  if (completeExpression !== null) return completeExpression;
  const lines = body.split(/\r?\n/);
  let last = lines.length - 1;
  while (last >= 0 && !lines[last].trim()) last -= 1;
  if (last <= 0) return body;
  const tail = lines[last].trim();
  const returned = expressionBody(tail);
  if (returned === null) return body;
  const prefix = lines.slice(0, last).join('\n');
  try {
    // A partial `let\nname = ...` can parse as a sloppy identifier expression.
    // Strict compilation rejects that ambiguity as well as incomplete syntax.
    // This is only a probe: the authored body keeps its original strictness.
    new AsyncFunction('"use strict";\n' + prefix);
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    return body;
  }
  if (/^[([`/+.-]/.test(tail)) {
    const end = prefix.trimEnd();
    if (!end.endsWith(';')) return body;
    // A semicolon hidden in a terminal line comment is not a statement end.
    // The otherwise invalid token is accepted only while still in that comment.
    try {
      new AsyncFunction(end + '\u0000');
      return body;
    } catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
    }
  }
  lines[last] = returned;
  return lines.join('\n');
};

prepareGuardedEval;
