# Contributing

English | [简体中文](CONTRIBUTING.zh-CN.md)

Contributions should preserve ABM's defining behavior: operate the user's real
browser session, prefer background page/CDP work, and use foreground physical
input only as an explicit last resort.

## Development setup

```text
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,desktop]"  # Windows PowerShell
python -m pip install -e ".[dev,desktop]"                  # other activated venvs
agent-browser-mcp extension-path
```

Load the printed directory as an unpacked extension. Python server changes are
picked up by an editable install; bridge changes require a bridge restart, and
extension source changes require a manual reload from the browser's extensions
page.

## Tests

The normal suite is offline and does not touch a browser:

```text
python -m ruff check src tests scripts
python -m pytest tests -q
python -m pytest tests -q --cov=agent_browser_mcp --cov-fail-under=85
python -m scripts.tool_coverage_report --format markdown
python -m scripts.check_tool_docs --format markdown
python -m scripts.versioning check
python -m build --wheel --sdist --outdir artifacts/dist
python -m scripts.check_distribution artifacts/dist
```

This is the order `scripts/finalize_change.py` and `.github/workflows/test.yml`
use, and the last two lines belong together: `check_distribution` inspects the
archives the build step just wrote, so running it on its own reports
`no wheel found` rather than a pass.

`ruff check` is the enforced rule set. `ruff format` is not a gate and most of
the existing sources are not format-clean, so running it across a file you are
only editing buries the change in unrelated reflows. Match the surrounding style
instead.

Live tests are opt-in:

```text
python -m pytest tests -q -m live
```

They drive a real connected browser and may temporarily affect the foreground.
Run them only on a prepared machine, record the initially active user tab, use
the shared scratch fixture, and verify cleanup/restoration afterward. Do not add
headless or Playwright fallback paths to live tests; those would test a different
product contract.

The public `test.yml` workflow runs only offline gates on GitHub-hosted runners.
`live.yml` is manual-only and targets a prepared self-hosted Windows runner. Set
the repository variable `ABM_LIVE_PYTHON` when that runner does not expose the
intended interpreter as `python`. Do not schedule the live workflow on a desktop
that is also used interactively. The live job is restricted to the canonical
repository and the `abm-live` GitHub environment. Configure that environment
with required-reviewer protection before registering the runner; the workflow
file cannot create or enforce repository environment protection rules itself.

For a local release candidate, run the complete offline pipeline on the exact
tree that will be published:

```text
python -m scripts.finalize_change --bump none --skip-live
python -m scripts.evidence_manifest --check
```

The finalizer first moves prior evidence into a timestamped
`artifacts/archive/` directory, then writes one canonical set containing offline
JUnit, coverage, tool evidence, and exactly one wheel/source archive pair.
`artifacts/evidence-manifest.json` binds those files to the Git HEAD, dirty
state, and public source-tree content hash. Any source edit or commit after that
run makes the evidence stale. Re-run the pipeline on the final commit before
publishing.

Two properties of that binding decide whether a report can be trusted:

- The manifest records a `schema_version`. When the fields that make up the
  fingerprint change, the checker rejects the older manifest **by version**
  instead of reporting a content mismatch, because a fingerprint computed from
  different inputs is not comparable. `re-seal with the current tooling` in the
  output means exactly that: rebuild the evidence, do not go looking for the
  edit that "changed" a file.
- A seal taken over a dirty tree records `git_dirty: true`, and the acceptance
  report treats that as an evidence problem. `git_head` alone does not identify
  the code that produced the artifacts when files are uncommitted or untracked,
  so a dirty seal can never reach `release_ready`. Commit the release surface
  first, then seal.

Let the finalizer write those artifacts. Running a gate by hand with
`--junitxml`/`--cov` pointed at `artifacts/` overwrites part of a sealed set and
leaves the rest, which `--check` then reports as a mismatch.

## Tool contract changes

When a tool name, parameter, default, or behavior changes, update all of these
in the same change:

1. `README.md` and `README.zh-CN.md` (the authoritative 55-tool table);
2. the tool's MCP `description=` text;
3. `docs/browser-mcp-default.SKILL.md` (the checked-in caller contract).

Maintainers also synchronize the installed copies of the caller skill. The
default documentation gate verifies the canonical repository copy, tool
registration, documented parameters, and version consistency. Maintainers can
add `--check-installed-skills` to verify the Agent, Codex, and Claude copies.

The caller skill is a source-repository maintenance contract, not Python package
runtime data. `docs/browser-mcp-default.SKILL.md` remains in Git for review and
synchronization but is deliberately excluded from wheel and source distribution
archives. Keep it free of machine-specific paths or claims.

## Version and release hygiene

- Keep the Python package, bridge protocol, extension manifest, READMEs, and
  latest changelog release synchronized. Use `python -m scripts.versioning
  check` before submitting a change. Add user-visible changes under
  `[Unreleased]`; `python -m scripts.versioning bump|sync` moves them into the new
  dated release and updates comparison links.
- Ordinary pull requests do not bump the release version. The CI increment gate
  applies only to pushes on `release/*` branches, where release coordination can
  own the shared version and changelog files without forcing every contributor
  into conflicts.
- `python -m scripts.finalize_change` synchronizes the requested target version before
  running the gate. Never edit a version after finalization without rerunning
  the complete test, coverage, documentation, build, and distribution pipeline
  on that exact tree.
- Do not commit caches, generated coverage, JUnit XML, logs, local screenshots,
  or build output. Keep generated and machine-local files covered by
  `.gitignore`.
- The legacy `src/agent_browser_mcp/chrome_extension/config.js`/TID page-command
  channel has been removed. That file must not exist in Git or Python
  distributions; the distribution gate rejects it.
- Never include bridge tokens, cookies, `.env` files, browser profiles, or
  copied user content. Run a secret scan over both the working tree and complete
  Git history before publishing to a public repository.
- Upload wheel and source-distribution files as GitHub Release assets. Do not
  commit them, local acceptance reports, or live-browser evidence to Git.

## Pull request checklist

- The diff is scoped to the stated behavior and preserves unrelated local work.
- Offline tests and documentation/version checks pass.
- New behavior has success, boundary, and cleanup coverage where applicable.
- User tabs are never enrolled into agent-owned cleanup.
- Background operations do not activate a tab or move the cursor.
- Public documentation is updated in both languages.
- No generated artifacts or secrets are included.
