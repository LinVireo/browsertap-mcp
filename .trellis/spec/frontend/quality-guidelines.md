# Browser Quality Gates

Use [CONTRIBUTING.md](../../../CONTRIBUTING.md) for required checks and
[validation contracts](../../../docs/agent-guides/validation.md) for coverage,
preflight, and artifact rules.

- `python -m scripts.lint_report` checks both extension and injected JavaScript
  through [eslint.config.mjs](../../../eslint.config.mjs). Keep both targets in
  coverage; use `npm ci` when the repository's ESLint dependencies are absent.
- Add focused regression coverage for changed dispatch, lifecycle, result, and
  cleanup behavior. See
  [test_extension_build.py](../../../tests/test_extension_build.py).
- Before live verification, identify the MCP, bridge, and extension builds.
  Apply the required restart or manual extension reload; matching version
  strings alone are insufficient.
- Page/CDP input stays in the selected background tab. Read
  [tabs and input](../../../docs/agent-guides/tabs-and-input.md) before changing
  physical fallback, hit testing, or focus restoration.
- Live tests operate the real browser and clean up only their own tabs. Report
  offline and live results separately.
