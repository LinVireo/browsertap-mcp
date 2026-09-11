# Remaining native acceptance

This task prepares the remaining evidence without changing SPEC rev7. Read the
current `SPEC.md` A6/A7 and use this integration's source and archive hashes,
not a candidate worktree's earlier CI results. Local integration outputs live in
`out/integration-20260911/`; the final handoff names the reviewed commit and
the wheel/sdist SHA-256 values.

## A7 capability gap

The registered desktop tool set is empty (`server._DESKTOP_TOOLS`). The only
remaining physical fallback is `resolve_leave_dialog`, limited to a lab-mode
leave-dialog recovery. Page screenshots, CDP file upload, and PDF generation
do not provide a native picker, print/save-dialog, extension-popup, browser-chrome,
or desktop-screenshot invocation with the A7 diagnostics.

Consequently A7 remains pending. Installing the desktop extra alone cannot
create the missing public capability. Preserve the removed global OS tools and
the current contract. A later task needs an explicit product decision: implement
one narrowly scoped supported desktop action, or revise and reapprove the
contract if that capability is no longer intended. This integration does neither.

Once an A7 capability exists, record all of the following in a new attestation:

1. Reviewed source and package hashes, platform, browser version, tool inventory,
   and `get_setup_status` showing an enforced `matches_tree` verdict.
2. A page/CDP attempt on an agent-owned test page, with before/after foreground
   identity and observed pointer behavior.
3. The explicit native-action request and exact result, including truthful
   `on_screen` and `input_quiet` diagnostics. Record an actual observation; do not
   infer either diagnostic from a passed page action.
4. The corresponding unverifiable/obscured-input refusal, recovery, and cleanup.

## macOS and Linux preparation

No native macOS/Linux browser has been exercised for this integration. Earlier
candidate CI proves only that candidate. The test-machine availability question
is still unanswered; lack of a response is not evidence that no machine exists.

On each native desktop, clone this integration's local source bundle into a new
directory, verify the source commit and the bundle/wheel/sdist SHA-256 values
against the handoff, and run from the cloned root. The source bundle retains Git
metadata required by the repository checks. The wheel/sdist are separate install
artifacts. Copy the supplied SPEC/PLAN context alongside the checkout for manual
acceptance. Use the machine's real Chrome/Edge profile and one
exclusive live owner. The desktop needs an interactive logged-in session;
headless CI and a Windows compatibility shell are not substitutes.

```sh
git clone --branch codex/r6-integration-20260911 /path/to/integration.bundle btap-native-check
cd btap-native-check
git rev-parse HEAD
python3 -m venv .venv
.venv/bin/python -m pip install '.[dev,desktop]'
npm ci --no-audit --no-fund
.venv/bin/python -m scripts.versioning check
.venv/bin/python -m scripts.lint_report
.venv/bin/python -m pytest tests -q --cov=browsertap_mcp --cov-fail-under=95 --cov-report=json:offline-coverage.json --junitxml=offline-junit.xml
.venv/bin/python -m scripts.check_tool_docs --format markdown
.venv/bin/python -m browsertap_mcp.cli extension-path
```

Load the printed unpacked extension directory in that profile and start a fresh
MCP process from this environment. After acquiring the machine's exclusive live
slot, start/restart only its managed BTAP bridge and verify its identity:

```sh
.venv/bin/python -m browsertap_mcp.cli bridge --restart
.venv/bin/python -m browsertap_mcp.cli doctor
.venv/bin/python -m pytest tests -q -m live --junitxml=native-live-junit.xml
```

Keep `artifacts/live-preflight.json`, the complete test output, OS/Python/browser
versions and archive hashes. The preflight must identify matching components,
enforce owned-tab cleanup, and record rather than judge user tab activity. A
refusal caused by missing native permissions is a reported gap, not a pass.

For A6, separately attest the ordinary page and heavy SPA workflow named by the
SPEC: explicit session/owner/generation, page reads/input, download, browser-layer
operation, foreground preservation and bounded recovery. For A7, follow the
capability prerequisites above; never fill in an attestation using CI results.

## Original wait delay

The bounded Windows experiment includes unchanged probes and probes wrapped only
with synchronous `performance` timing. Its one-second wait budget can yield a
late receipt despite sub-5-ms page computation. Keep those samples alongside the
initial failed short-budget sample. They prove the release/collection path on the
current runtime, not the original historical delay's cause. A future reproduction
needs timestamps at extension dispatch/reply and bridge WebSocket receipt before
attributing the remaining span to one layer. Do not repeat navigation or input
to discover whether a previous uncertain action executed.
