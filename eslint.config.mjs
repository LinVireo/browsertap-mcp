import js from "@eslint/js";
import globals from "globals";

// The extension is the half of the deliverable no Python linter can see: ~4.9k
// lines that ship inside the wheel and run in the user's browser. Everything
// here is `eslint:recommended` with two rules tuned to one deliberate idiom,
// and nothing suppressed inline.
export default [
  {
    // `build/` and `.superpowers/` hold older copies of these same files. Both
    // are gitignored, and eslint honours .gitignore for a directory walk -- but
    // not for an explicitly named path, so the ignore is restated rather than
    // relied on.
    ignores: ["build/**", ".superpowers/**", "node_modules/**"],
  },
  {
    files: ["src/browsertap_mcp/chrome_extension/*.js"],
    ...js.configs.recommended,
    languageOptions: {
      ecmaVersion: 2023,
      // Not "module": these load as classic scripts (an MV3 service worker
      // without `"type": "module"` in the manifest, plus two content scripts),
      // so top-level `await` and `import` genuinely are errors here.
      sourceType: "script",
      globals: {
        ...globals.browser,
        ...globals.serviceworker,
        ...globals.webextensions,
      },
    },
    rules: {
      ...js.configs.recommended.rules,
      // `try { ... } catch (_) {}` is this codebase's swallow-and-continue, and
      // it is load-bearing: a `chrome.*` call whose worker was collected throws
      // a bare `No SW`, which is routine rather than a failure (AGENTS.md s8).
      // The rules stay on for every other shape -- an ignored error still has
      // to be named `_`, so "I meant to drop this" is visible at the catch.
      "no-empty": ["error", { allowEmptyCatch: true }],
      "no-unused-vars": ["error", { caughtErrorsIgnorePattern: "^_$" }],
    },
  },
  {
    // The scan_page payload. Not extension code: these are injected into the
    // page the user is looking at, so `chrome.*` is absent and only the plain
    // browser globals apply -- a config that handed them `globals.webextensions`
    // would stop `no-undef` from catching a `chrome.` call that cannot work
    // there.
    files: ["src/browsertap_mcp/page_scripts/*.js"],
    ...js.configs.recommended,
    languageOptions: {
      ecmaVersion: 2023,
      // Injected fragments, evaluated as a classic script body.
      sourceType: "script",
      globals: { ...globals.browser },
    },
    rules: {
      ...js.configs.recommended.rules,
      "no-empty": ["error", { allowEmptyCatch: true }],
      "no-unused-vars": ["error", { caughtErrorsIgnorePattern: "^_$" }],
    },
  },
];
