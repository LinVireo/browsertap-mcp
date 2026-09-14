# Validation contracts

Read before changing test preflight, browser cleanup, coverage checks, lint
coverage, line-ending policy, or evidence and distribution file rosters.

Start with [AGENTS.md](../../AGENTS.md) for the task entry point. Numbered
sections retain the original guide's references; other section numbers refer
to that root guide. Commands and release gates remain in
[CONTRIBUTING.md](../../.github/CONTRIBUTING.md).

## 7. Running the tests

```bash
# offline: no browser, no bridge, no network. Under two minutes.
python -m pytest tests/ -q

# live: needs the bridge running and the extension connected. Four minutes or so.
python -m pytest tests/ -q -m live
```

For release evidence, the finalizer and CI use
`python -m scripts.test_run_evidence --mode offline` (or `--mode live`). This
collects the whole `tests` directory independently before execution. Both
receipts record the full collection before marker selection, the actual pytest
arguments, source fingerprints, and collection outcomes; the execution receipt
also records each setup/call/teardown result. JUnit carries the matching node IDs.
The seal binds all three files for each mode. Missing or duplicate tests,
subset invocations, skipped collection, stale source, and truncated XML fail
validation even when the remaining JUnit cases pass. Existing outputs must be
archived first; the finalizer does that recoverably.

`python -m scripts.acceptance_report --check` is a read-only report check. It
validates the seal, recomputes the report from those inputs, compares the exact
bytes, and requires all release gates to pass. The report uses the seal's
timestamp and canonical manifest digest, so unchanged inputs reproduce the
same bytes and re-sealed inputs cannot hide behind an unchanged score. The
report is excluded from its own manifest to avoid a circular hash;
`scripts.evidence_manifest --check` alone verifies only the inputs.

Three things decide whether the live run means anything, and skipping any of them
wastes the whole run:

- **The bridge and the extension have to be running this checkout.** Both are
  long-lived and keep whatever build they started with (section 1), so a green
  live run can certify code that is not in the tree -- and until the fixture
  checked it, nothing downstream recorded which build had answered, so a stale
  extension could be sealed as a release. The session fixture now asks
  `get_setup_status()` before it samples anything and fails the run naming every
  stale component with its one fix. There is no override for this one, and it is
  a failure rather than a skip on purpose:
  `tests/live_preflight.stale_component_reason` says why.
- **Leave the browser alone if you can.** With someone opening and closing tabs,
  failover can pick a tab that is still loading and the CDP fallback loses its
  debugger mid-command. Measured: browser in use → 8 minutes and one failure;
  idle browser → 4 m 15 s, 54 passed. This is advice about noise, not a
  precondition: the preflight records what the browser did and never refuses or
  skips over it, because the user's tabs are the user's. A busy browser used to
  skip the whole live layer, which read someone else's tabs to decide whether the
  suite may run and left machines whose browser is never idle with no live layer
  at all.
- **Do not work on the machine during the coverage round.** Instrumentation
  roughly doubles the wall clock. The tests that used to fail as a group there
  now assert the budget the code hands downstream instead of the elapsed time,
  so the class is mostly gone -- but a remaining wall-clock assertion is a loose
  backstop, not a performance target. Re-run on an idle machine before treating
  a timing failure as a defect.

The live suite **touches the human's foreground tab**. Note the active tab before
you start and put it back afterwards:

```python
[t for t in S.list_all_tabs()["data"] if t["active"]]
```

Do not remember the id you get back -- resolve it again with
`tests/live_preflight.resolve_remembered_tab`, for the reason in section 3. It
returning `None` means the human closed that tab, which is theirs to do and fails
nothing.

The `scratch_session` fixture opens one temporary tab that every live test
shares and closes it at the end. Do not change it to one tab per test; that
makes a mess of a real person's browser.

**The one thing the live layer fails a run over is a tab it opened and did not
close.** That verdict comes from `server._TAB_OWNERSHIP.outstanding()`, not from
a diff of the browser, and the difference is the whole point: a diff cannot say
who opened a tab, so the check it replaced fired identically for a human opening
a tab, a human closing one, and the suite leaking its own scratch tab. Three
things must survive an edit here:

- **Never widen it back to the whole browser.** The user may open, close and
  navigate their own tabs mid-run; that is recorded as `tab_activity` and judged
  not at all.
- **`own_tabs.enforced` is computed from the ownership counters**, so "nothing
  leaked" can be told apart from "this process opened no tabs, so nothing was
  measured" -- the same field the quiet-input gate needed for the same reason.
- **Both causes stay in the message.** A forgotten `close_tabs(..., owner_id=)`
  and a close refused with `lifecycle generation changed` (Chrome discarded the
  suite's own tab and `close_tabs` correctly would not close its replacement)
  leave identical registry records and need different fixes.

To assert that a tab really came to the front, use the `active` field from
`list_all_tabs()`. Do **not** use `document.visibilityState`: it also reports
`hidden` when the window is merely minimised, and a test cannot control that, so
the assertion turns flaky.

## 8. Odds and ends

- **The extension JavaScript is linted now**, which it was not for most of this
  repository's life -- `npm ci` once, then the ordinary `python -m
  scripts.lint_report` covers it (the gate itself is CONTRIBUTING.md's section).
  Two eslint rules are *tuned* rather than obeyed, because the idiom they would
  flag is load-bearing: `catch (_) {}` around a `chrome.*` call is how the
  routine `No SW` above gets swallowed, so `allowEmptyCatch` is on and an unused
  catch binding named exactly `_` is permitted. Write `catch (_)` when you are
  deliberately ignoring the error and name the binding only when you read it --
  `catch (e) {}` with `e` unused fails the gate. Do not reach for an inline
  `eslint-disable` instead; there is not one in the tree, and the first one makes
  every later reader wonder which rules still mean something.

- **A rule count sampled from one lint target cannot vouch for another.** eslint
  has a third vacuous-pass shape that ruff does not: a flat config whose `files:`
  pattern stops matching still walks the directory, exits 0 and reports every
  file clean while applying **zero** rules -- `files_scanned` cannot see it, and
  only `eslint --print-config` can. `_rules_applied` in `scripts/lint_report.py`
  asks that question **once per target** and reports the **minimum**, so
  `rules_applied > 0` means every target is covered rather than at least one.
  Sampling the first target was sound while `JS_LINT_TARGETS` held one entry and
  became a vacuous pass the moment `page_scripts` was added: the new directory
  would have inherited the extension's rule count and read as enforced while
  enforcing nothing. Adding a third target means adding a config block for it in
  `eslint.config.mjs` in the same change -- the floor is what turns forgetting
  that into a red gate instead of a silent one.

- **Line endings are a correctness property here, not a formatting one.** The
  release seal hashes the *raw bytes* of every tracked file
  (`evidence_manifest.source_identity`), so a CRLF working copy gives `content_sha256` a
  value nobody can reproduce -- not even from a clone of the named commit. Two different
  tests in `tests/test_evidence_manifest.py` hold this down. One asks **git** whether an
  `eol` attribute governs every tracked path: `.gitattributes` is a single
  `* text=auto eol=lf` rule because the suffix list it replaced kept losing (`*.mjs`,
  `*.yml`, `*.toml` appended late; extensionless files like `LICENSE` uncoverable) --
  measured at 0.4.15: five tracked paths ungoverned, clone vs worktree differing in
  three. The other fails if any tracked text file in the worktree holds CRLF: the
  attribute governs a *checkout*, but a local tool can undo it afterwards (Windows
  `Path.write_text` translates on write; a `ruff format` with `line-ending = auto` once
  did it to 21 files at once). Neither test can satisfy the other -- do not fold them.

- **A gate can only ask about the file set it was told about**, and browser-side code
  now lives in two directories rather than one. Measured twice in the same release, both
  times because a check enumerated `chrome_extension/` while `page_scripts/` had just
  been carved out of `simphtml.py`'s string literals:
  `test_manifest_declares_the_chrome_floor_its_own_api_use_forces` derives
  `minimum_chrome_version` from the browser APIs actually called and read
  `chrome_extension/*.js` only, so the tree's highest floor (Chrome 121, for the
  `Element.checkVisibility()` option names) sat outside what the gate could see while the
  manifest said 111; and `extension_build`'s digest walks the whole directory rather than
  a list of names for the same reason. Both gates assert their own file sets now, which is
  what will catch browser-side code added in a third place --
  `test_no_browser_side_file_ships_without_the_suite_knowing` compares
  `SHIPPED_EXTENSION_FILES` to the directory in both directions, so a new file there is a
  red gate rather than a silent arrival.
