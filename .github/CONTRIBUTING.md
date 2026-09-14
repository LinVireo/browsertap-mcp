# Contributing

English | [简体中文](CONTRIBUTING.zh-CN.md)

This guide is for people and coding agents changing or releasing BTAP. To
install and use it, start with [README.md](../README.md). Agents calling browser
tools use the packaged skills; agents editing this repository also read
[AGENTS.md](../AGENTS.md) for implementation invariants.

Contributions should preserve BTAP's defining behavior: operate the user's real
browser session, prefer background page/CDP work, and use foreground physical
input only as an explicit last resort.

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md). One
clause matters more here than in most projects: this tool drives a real browser
profile, so redact cookies, tokens, history, and page screenshots before putting
a reproduction in an issue or pull request.

## Development setup

From the repository root, create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

On Linux/macOS, use `python -m venv .venv` followed by `. .venv/bin/activate`.
Then install the development dependencies in that environment:

```text
python -m pip install -e ".[dev,desktop]"
browsertap extension-path
```

If activation is unavailable, use the virtual environment's full executable
paths for the commands below. Load the printed directory as an unpacked
extension. The editable install points at this checkout; restart the MCP session
to load Python server changes. Bridge changes require a bridge restart, and
extension source changes require a manual reload from the browser's extensions
page.

### Commit hooks

The optional [pre-commit](https://pre-commit.com/) configuration runs the
repository's lint, tool-documentation, version, staged-whitespace and secret
checks. Install Gitleaks on your `PATH`, run `npm ci`, and use the activated
development environment:

```text
python -m pip install "pre-commit>=3.2,<5"
pre-commit install
pre-commit run --all-files
```

Local hooks use tools from that environment; they do not install a second lint
toolchain. Missing tools fail the hook. The lint report is written to
`out/pre-commit/lint.json`, and the next check requires enforced JavaScript and
type results. Gitleaks scans the staged diff even with `--all-files`.
The hooks do not run tests, builds, live browser actions or release sealing;
run the relevant checks below before committing.

## Tests

The default suite is offline: it does not drive the user's browser. Run these
checks from the repository root with the development environment's Python:

```text
npm ci
python -m scripts.lint_report
python -m pytest tests -q
python -m pytest tests -q --cov=browsertap_mcp --cov-fail-under=95
python -m scripts.tool_coverage_report --format markdown
python -m scripts.check_tool_docs --format markdown
python -m scripts.versioning check
python -m build --wheel --sdist --outdir artifacts/dist
python -m scripts.check_distribution artifacts/dist
python -m scripts.check_install artifacts/dist --no-deps
```

Use an empty build output directory. Build before checking or installing the
archives; `check_distribution` expects exactly one wheel and one sdist and
compares their package-file sets. This catches retired files left in a stale
`build/` tree as well as files omitted from either archive.

The last command is a **layout-only** check for environments without dependency
downloads. With index access, also run
`python -m scripts.check_install artifacts/dist`: it installs dependencies in a
fresh environment, outside the checkout, then exercises the CLI and packaged
paths. Check `mode` and `proves_cli`; a `--no-deps` pass does not prove the CLI runs.

### What the gates prove

| Gate | Acceptance |
| --- | --- |
| Tests | Success, failure and cleanup paths behave as asserted. Add regression cases for changed behavior. |
| Coverage | The combined line-and-branch total is at least 95%; this is not a promise that each metric or file reaches 95%. Acceptance also checks `PER_FILE_COVERAGE_FLOOR` and rejects missing per-file data. |
| Ruff | Python lint over `src/`, `tests/`, and `scripts/`. Each target must contribute files. |
| ESLint | Both extension and injected page JavaScript are scanned; enabled rules are checked per target. |
| mypy | Every shipped Python source is checked, including unannotated function bodies. Missing or incomplete checks fail. |
| Documentation | Registered tools, parameters, defaults, both READMEs, caller skills, and versions agree. |
| Distribution / installation | Required files are packaged, local data is excluded, and the installed package is usable when the full install check runs. |

`scripts/lint_report.py` is the shared entry point for Ruff, ESLint and mypy.
It records separate results in `artifacts/lint.json` (`javascript` and
`types` alongside Python lint), including file counts and enforcement.
Run `npm ci` for ESLint and install `.[dev]` for mypy. Missing JavaScript
tooling reports `unavailable`; the command can still pass the Python checks,
but release acceptance rejects an unenforced JavaScript gate. Missing mypy,
type errors or an incomplete type check make the command fail.

Use `python -m mypy src` for a focused type check. Match the surrounding code
style; `ruff format` is not a gate and whole-file formatting obscures small
changes in this codebase.

### Live tests

```text
python -m pytest tests -q -m live
```

Live tests require a prepared real browser and can affect its foreground.
Use the shared scratch fixture, not one tab per test; headless or Playwright
fallbacks would test a different product.

The session fixture uses `get_setup_status()` and `tests/live_preflight.py` to
check that the MCP process, bridge and extension match the tested tree. Stale
components fail the run and name the required restart or manual reload.
Browser activity is recorded as context, not used to forbid the user from
opening, closing or navigating their own tabs.

Cleanup checks `server._TAB_OWNERSHIP.outstanding()`: only tabs opened by the
suite count as leaks. A failure names both possible causes: missing owner-aware
cleanup or a close refused with `lifecycle generation changed`.
`own_tabs.enforced` distinguishes a real check from a run that owned no tabs.
Build identity, activity and ownership counters are written to
`artifacts/live-preflight.json` and bound alongside the live JUnit evidence.

`test.yml` runs offline on GitHub-hosted runners. `live.yml` is manual-only,
restricted to the canonical repository and the `btap-live` environment on a
prepared self-hosted Windows runner. Configure required reviewers before using
that runner; the workflow cannot create environment protection rules. Set
`BTAP_LIVE_PYTHON` when `python` resolves to the wrong interpreter.

### Release evidence

Use the finalizer when preparing a release candidate, not while iterating on a
moving worktree:

```text
python -m scripts.finalize_change --bump none --skip-live
python -m scripts.evidence_manifest --check
python -m scripts.check_release_tag --allow-missing-tag
```

The version must already be appropriate for `--bump none`; see
[Version and release hygiene](#version-and-release-hygiene).
`--skip-live` does not establish live verification or release readiness.

The finalizer archives previous evidence under `artifacts/archive/`, then
generates the canonical reports and distribution pair.
`artifacts/evidence-manifest.json` binds them to the Git HEAD, dirty state and
public file hashes. Commit the intended release surface before sealing:
`git_dirty: true` cannot reach `release_ready`. Any later edit or commit requires
new evidence; a manifest schema mismatch also requires regeneration with the
current tooling.

Preserve existing sealed artifacts. For ad hoc runs, write reports under a new
`out/` subdirectory rather than overwriting individual files in `artifacts/`.

## Tool contract changes

When a tool name, parameter, default, or behavior changes, update all of these
in the same change:

1. `README.md` and `README.zh-CN.md` (the authoritative 51-tool table);
2. the tool's MCP `description=` text;
3. `src/browsertap_mcp/skills/browsertap-default/SKILL.md` (the caller
   contract: which tool to call first, when `session_id` is mandatory);
4. `src/browsertap_mcp/skills/browsertap-bridge-recovery/SKILL.md` (what a caller
   follows when the bridge itself is unreachable).

The two skills cross-reference each other, so each is hash-checked as its own
group of copies — a reader who receives an update for only one of them gets
pointed at advice that no longer matches. Keep both free of machine-specific
paths or claims; `tests/test_documentation_contract.py` fails on an absolute path.

They ship as package data, so `pip install browsertap-mcp` carries them and
`browsertap skill-path` prints the directory that holds them as
`<name>/SKILL.md`. Both the `MANIFEST.in` rule and the `package-data` glob in
`pyproject.toml` are required: they are what put the files in the source archive
and the wheel respectively, and having only one produces an sdist that carries
the skills and a wheel that does not — which is the half `pip install` uses.
`scripts/check_distribution.py` requires them in both archives. The source
archive also includes the two plugin discovery copies under root `skills/`;
other caller copies and private agent configuration are rejected.

### Plugin distribution

Keep `src/browsertap_mcp/skills/` canonical. After editing a public Skill, run
`python -m scripts.sync_plugin_skills`; use `--check` to detect drift without
writing. The plugin regression tests enforce byte equality. Both host manifests
launch this plugin's source with uv, and `scripts.versioning` keeps their versions
aligned with the package and extension. Publishable metadata is limited to
`.claude-plugin/`, `.codex-plugin/`, and `.agents/plugins/marketplace.json`.

Run `python -m pytest tests/test_plugins.py tests/test_versioning.py
tests/test_distribution_contract.py -q` and both hosts' manifest validators.
Use a freshly built source archive and temporary host configuration directories
for marketplace installation tests; a working tree can contain private
`.mcp.json`, hooks, or agent instructions that must not enter a plugin cache.
See [plugin setup](../docs/PLUGINS.md) for the host commands.

Point a skill manager at the shipped directory instead of copying the files. A
copy reads as correct for as long as the contents agree and then silently stops
receiving updates; that is the drift the hash check exists to catch. If you do
keep copies, pass `--check-installed-skills` together with the directories that
hold them:

```bash
python -m scripts.check_tool_docs --check-installed-skills \
    --skill-mirror /path/to/installed/skills
# or: BROWSERTAP_SKILL_MIRRORS="dir1:dir2" python -m scripts.check_tool_docs --check-installed-skills
```

Each directory is expected to contain `<skill-name>/SKILL.md`. Where an agent
client installs its skills is machine configuration, so this repository does not
record those paths; requesting the comparison without naming a directory fails
rather than silently passing. The default gate — no flag — checks the shipped
copies, tool registration, documented parameters and defaults, and version
consistency, which is everything a contributor without installed copies can
verify.

## Version and release hygiene

- Keep both plugin manifests, the Python package, bridge protocol, extension manifest, READMEs, the MCP
  Registry manifest (`server.json`) and the latest changelog release
  synchronized. Use `python -m scripts.versioning check` before submitting a
  change. Add user-visible changes under `[Unreleased]`;
  `python -m scripts.versioning bump|sync` moves them into the new dated release
  and updates comparison links.
- `server.json` counts as a production file, alongside `src/` and `scripts/`, so
  editing it requires a version increment. It states the version twice -- the
  top-level one is the label the registry displays, the one in `packages[]` is
  what a client installs -- and both are rewritten and compared, because a bump
  that moved only the label would list one release and hand over another. Its
  `name` has to match the `mcp-name` marker in README.md, which is the pair the
  registry accepts as proof that the namespace belongs to this repository.
- Ordinary pull requests do not bump the release version. The CI increment gate
  applies only to pushes on `release/*` branches, where release coordination can
  own the shared version and changelog files without forcing every contributor
  into conflicts.
- `python -m scripts.finalize_change` runs the same increment check locally,
  against the last release tag and against the **working tree**. CI compares a
  push against the commit before it, which never fires in a repository that has
  not been pushed, and a commit-range comparison sees nothing at all while a
  change set is still uncommitted — so a round of real behaviour changes could be
  finalized under the number it started with. The local check refuses that. It
  reports the baseline it used and skips only when the repository has no tag yet.
- `python -m scripts.finalize_change` synchronizes the requested target version before
  running the gate. Never edit a version after finalization without rerunning
  the complete test, coverage, documentation, build, and distribution pipeline
  on that exact tree.
- Do not commit caches, generated coverage, JUnit XML, logs, local screenshots,
  or build output. Keep generated and machine-local files covered by
  `.gitignore`.
- The legacy `src/browsertap_mcp/chrome_extension/config.js`/TID page-command
  channel has been removed. That file must not exist in Git or Python
  distributions; the distribution gate rejects it.
- Never include bridge tokens, cookies, `.env` files, browser profiles, or
  copied user content. `.github/workflows/supply-chain.yml` scans both the
  working tree and the complete Git history on every push, using a gitleaks build
  pinned by version *and* sha256. Run the same two scans locally before
  publishing to a public repository:

  ```bash
  gitleaks git . --no-banner --redact
  gitleaks dir . --no-banner --redact
  ```

  The history half is the one that matters after the fact: a secret that was
  committed and later deleted is still published, and only rewriting history
  removes it -- another commit does not. `--redact` keeps the scanner from
  printing the secret it found into build output that anyone can read.
- Every third-party action is pinned to a commit SHA, with the version it
  corresponds to in a trailing comment, and `tests/test_supply_chain.py` refuses
  a floating tag apart from the one documented exception. A SHA has no update
  channel of its own, so `.github/dependabot.yml` is that channel: the actions
  ecosystem only, monthly, grouped into one pull request. The Python side is
  deliberately absent -- the audit above already covers it, and the upper bounds
  on the `dev` extra exist to stop the gate toolchain moving on its own, so a bot
  raising them would undo the thing they are for.
- The same workflow resolves the closure a plain `pip install` produces, audits
  it against the advisory database, and publishes a CycloneDX SBOM as a build
  artifact. That audit is informational there and blocking in `release.yml`: an
  advisory published overnight should not turn every branch red, but it is a
  perfectly good reason not to ship a new version.
- Upload wheel and source-distribution files as GitHub Release assets. Do not
  commit them, local acceptance reports, or live-browser evidence to Git.

## Publishing to PyPI

Publish the package as [`browsertap-mcp`](https://pypi.org/project/browsertap-mcp/).
Verify the core installation and the optional `desktop` extra separately;
ordinary page and browser workflows must work without desktop dependencies.

`.github/workflows/release.yml` builds, gates, and uploads. It never runs on a
push: the triggers are a manual run and a published GitHub Release. The reason
is that an upload cannot be undone — a filename on PyPI can never be reused, so
a bad upload burns that version number permanently and the only fix is to
release the next patch.

Three things have to exist before the workflow can upload, and none of them can
be created from inside this repository:

1. A PyPI account that owns the project name `browsertap-mcp`. This one is
   already settled: the 0.4.12 upload created the project, and a name in use by
   someone else could not have been taken over.
2. A **Trusted Publisher** on PyPI for this repository
   (`LinVireo/browsertap-mcp`), workflow `release.yml`, environment `pypi`.
   Trusted Publishing means the workflow exchanges a short-lived GitHub OIDC
   token for the upload credential at request time, so no API token is stored in
   the repository — there is nothing to leak and nothing to rotate. Repeat the
   same setup on TestPyPI with environment `testpypi`.
3. GitHub environments named `pypi` and `testpypi`. Add a required reviewer to
   `pypi`: the environment is the last point at which a human confirms an upload
   that cannot be reversed.

Then, in order:

```bash
# 1. Prove the tree and the archives are releasable, locally.
python -m scripts.finalize_change --bump none
python -m scripts.evidence_manifest --check

# 2. Rehearse on TestPyPI (Actions -> BTAP publish to PyPI -> index: testpypi),
#    then install from there into a throwaway virtual environment. Dependencies
#    come from the real index; only this package comes from the rehearsal one.
python -m pip install --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ "browsertap-mcp[desktop]"

# 3. Publish for real by publishing the GitHub Release for the tag.
```

The workflow re-runs the offline suite, the documentation checks,
`scripts.check_distribution` (required files, no machine-local data, and the
metadata the index needs to render and classify the release) and
`twine check --strict` against the exact archives it is about to upload. Run
`python -m twine check --strict dist/*` locally too if you built archives by
hand: it is the only check that renders the long description the way the index
will, and the last one that is still free.

It then installs the wheel it just built into a throwaway virtual environment and
runs the console script there (`scripts.check_install`), audits the dependency
closure that wheel pulls onto a user's machine, and writes a CycloneDX SBOM of
that closure as a separate artifact -- separate because the publish job uploads
everything under `dist/` to the index.

A tag pointing at a commit other than the one the acceptance evidence was sealed
on publishes something nobody verified. `python -m scripts.check_release_tag`
answers that mechanically: it fails when `v<source version>` does not exist,
when it points at another commit -- naming both shas, the distance, and which
production files differ -- or when production files are still uncommitted, since
no tag can describe a file that is not in a commit. `release.yml` runs it before
it installs or builds anything. Run it yourself after tagging and before
publishing the Release, and confirm the sealed report's `verified_at` commit is
that same commit.

## Listing on the MCP Registry

`server.json` is the listing. The registry stores metadata only, never archives,
so it can be submitted only **after** the PyPI upload for that exact version
exists -- the version in `packages[]` is resolved against the index.

Ownership of a PyPI package is proved by an `mcp-name: <server name>` string in
the README that becomes the package description on PyPI, which for this project
is `README.md` (`readme = "README.md"` in `pyproject.toml`). It is line 1, inside
an HTML comment, and `server.json`'s `name` has to match it exactly;
`tests/test_documentation_contract.py` checks that pair. The marker must be
followed by a boundary, so keep it on its own line -- gluing a full stop onto the
end stops the match. GitHub-based authentication additionally requires the name
to start with `io.github.<owner>/`.

The registry's own `mcp-publisher` CLI does the submission: `login` (GitHub is
one of several supported methods), `validate` to check the manifest against the
published schema without publishing, then `publish` from the repository root.
Run `validate` first; it is the only place the JSON Schema itself is enforced,
because this repository gates the fields it can prove locally -- the two version
copies, the identifier, the registry type and the repository URL -- and does not
vendor the schema.

The registry is in preview and says data resets are possible before general
availability, so treat a successful listing as re-doable rather than permanent.
A new release needs a new submission: bump, release to PyPI, publish again.

## Pull request checklist

- The diff is scoped to the stated behavior and preserves unrelated local work.
- Offline tests and documentation/version checks pass.
- New behavior has success, boundary, and cleanup coverage where applicable.
- User tabs are never enrolled into agent-owned cleanup.
- Background operations do not activate a tab or move the cursor.
- Public documentation is updated in both languages.
- No generated artifacts or secrets are included.
