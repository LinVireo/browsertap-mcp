# Contributing

English | [简体中文](CONTRIBUTING.zh-CN.md)

Contributions should preserve BTAP's defining behavior: operate the user's real
browser session, prefer background page/CDP work, and use foreground physical
input only as an explicit last resort.

## Development setup

```text
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,desktop]"  # Windows PowerShell
python -m pip install -e ".[dev,desktop]"                  # other activated venvs
browsertap extension-path
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
python -m pytest tests -q --cov=browsertap_mcp --cov-fail-under=85
python -m scripts.tool_coverage_report --format markdown
python -m scripts.check_tool_docs --format markdown
python -m scripts.versioning check
python -m build --wheel --sdist --outdir artifacts/dist
python -m scripts.check_distribution artifacts/dist
python -m scripts.check_install artifacts/dist --no-deps
```

This is the order `scripts/finalize_change.py` and `.github/workflows/test.yml`
use, and the last three lines belong together: the build writes the archives that
`check_distribution` reads and `check_install` installs, so running either on its
own reports `no wheel found` rather than a pass.

`check_distribution` and `check_install` answer different questions.
`check_distribution` reads what is *inside* the archive; `check_install` puts the
wheel into a throwaway virtual environment with no repository on the path and
exercises it there, which is the only way "does `pip install browsertap-mcp`
leave a stranger with something that runs" stops being a guess. Locally it is
`--no-deps`: no index access, so it proves the layout only -- metadata version,
console script, packaged skills, extension files. CI runs it without that flag
and really executes `browsertap --version`, `skill-path` and `extension-path`.
The report's `mode` and `proves_cli` fields say which of the two ran, so a
layout-only pass is never read as "the CLI works".

`ruff check` is the enforced rule set. `ruff format` is not a gate and most of
the existing sources are not format-clean, so running it across a file you are
only editing buries the change in unrelated reflows. Match the surrounding style
instead.

Live tests are opt-in:

```text
python -m pytest tests -q -m live
```

They drive a real connected browser and may temporarily affect the foreground.
Run them only on a prepared machine and use the shared scratch fixture rather
than opening a tab per test. Do not add headless or Playwright fallback paths to
live tests; those would test a different product contract.

Two preconditions used to be written here and left to a human to keep: nobody may
be using the browser while the suite runs, and the tab inventory has to come out
the way it went in. A third was written in the agent notes instead: the bridge
daemon and the extension have to be running this checkout. The session fixture in
`tests/conftest.py` now enforces all three (`tests/live_preflight.py` holds the
reasoning):

- Before anything else it asks `get_setup_status()` what build each of the three
  processes is running. The bridge daemon and the extension are long-lived and
  keep whatever build they started with, so a live pass can certify code that is
  not in the tree -- and until this check existed, no gate and no sealed artifact
  recorded which build had answered. A skew fails the run before the browser is
  even sampled, naming every stale component with the one step that fixes it.
  This one has no override: reloading the extension is a single click and
  restarting the bridge is a single command.
- Before the first live test it samples the tab list twice, 1.5s apart. If a tab
  was opened, closed, navigated or focused in between, someone is using that
  browser and the whole live layer is skipped rather than run against a moving
  target. A skip is not a pass -- `scripts/acceptance_report.py` fails the live
  gate on any skipped case.
- After the last one it compares the inventory against that baseline. A tab the
  suite left behind, closed, or navigated fails the run in teardown. The
  foreground moving does not: the suite raises tabs on purpose.
- Every verdict, the build each of the three processes was running, and whether
  the browser was idle at all, are written to `artifacts/live-preflight.json`,
  which `live.yml` uploads with the junit.

Set `BTAP_LIVE_ALLOW_BUSY_BROWSER=1` to run anyway on a machine that is never
idle; the end-of-run check drops to a warning and the report records that the
evidence was produced against a browser in use.

The public `test.yml` workflow runs only offline gates on GitHub-hosted runners.
`live.yml` is manual-only and targets a prepared self-hosted Windows runner. Set
the repository variable `BTAP_LIVE_PYTHON` when that runner does not expose the
intended interpreter as `python`. Do not schedule the live workflow on a desktop
that is also used interactively. The live job is restricted to the canonical
repository and the `btap-live` GitHub environment. Configure that environment
with required-reviewer protection before registering the runner; the workflow
file cannot create or enforce repository environment protection rules itself.

For a local release candidate, run the complete offline pipeline on the exact
tree that will be published:

```text
python -m scripts.finalize_change --bump none --skip-live
python -m scripts.evidence_manifest --check
python -m scripts.check_release_tag --allow-missing-tag
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
`scripts/check_distribution.py` requires them in both archives and refuses a
`SKILL.md` anywhere else in either one.

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

- Keep the Python package, bridge protocol, extension manifest, READMEs, and
  latest changelog release synchronized. Use `python -m scripts.versioning
  check` before submitting a change. Add user-visible changes under
  `[Unreleased]`; `python -m scripts.versioning bump|sync` moves them into the new
  dated release and updates comparison links.
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
- The same workflow resolves the closure a plain `pip install` produces, audits
  it against the advisory database, and publishes a CycloneDX SBOM as a build
  artifact. That audit is informational there and blocking in `release.yml`: an
  advisory published overnight should not turn every branch red, but it is a
  perfectly good reason not to ship a new version.
- Upload wheel and source-distribution files as GitHub Release assets. Do not
  commit them, local acceptance reports, or live-browser evidence to Git.

## Publishing to PyPI

The package is not on PyPI yet. `pip install browsertap-mcp` therefore does
not work, and both READMEs say so; that sentence changes only once the upload
has actually happened.

`.github/workflows/release.yml` builds, gates, and uploads. It never runs on a
push: the triggers are a manual run and a published GitHub Release. The reason
is that an upload cannot be undone — a filename on PyPI can never be reused, so
a bad upload burns that version number permanently and the only fix is to
release the next patch.

Three things have to exist before the workflow can upload, and none of them can
be created from inside this repository:

1. A PyPI account with the project name `browsertap-mcp` available or already
   owned. Check <https://pypi.org/project/browsertap-mcp/> first; a name in
   use by someone else cannot be taken over.
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

## Pull request checklist

- The diff is scoped to the stated behavior and preserves unrelated local work.
- Offline tests and documentation/version checks pass.
- New behavior has success, boundary, and cleanup coverage where applicable.
- User tabs are never enrolled into agent-owned cleanup.
- Background operations do not activate a tab or move the cursor.
- Public documentation is updated in both languages.
- No generated artifacts or secrets are included.
