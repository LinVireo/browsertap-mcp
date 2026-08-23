"""Supply-chain gates: what ever reached a commit, and what a plain install pulls in.

These are plain-text assertions over the workflow files, the same way the other
CI wiring is pinned in this suite. They are not a substitute for running the
workflow -- they exist so that the properties that make each gate meaningful
(full history, a pinned scanner, a redacted report, an SBOM that cannot leak into
the upload directory) cannot be dropped by an edit that still looks reasonable.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUPPLY_CHAIN = (ROOT / ".github" / "workflows" / "supply-chain.yml").read_text(encoding="utf-8")
RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
SECRETS_JOB = SUPPLY_CHAIN.split("dependencies:", 1)[0]


def test_the_secret_scan_covers_the_history_and_the_working_tree():
    """A deleted secret is still published, so the worktree scan alone proves little."""
    assert "gitleaks git ." in SECRETS_JOB
    assert "gitleaks dir ." in SECRETS_JOB


def test_the_history_scan_is_given_a_full_clone():
    """With the default shallow checkout the history scan would scan one commit."""
    assert "fetch-depth: 0" in SECRETS_JOB


def test_the_scanner_is_pinned_by_version_and_by_digest():
    """An unpinned scanner is a third party that can change between green runs."""
    assert re.search(r"GITLEAKS_VERSION: \d+\.\d+\.\d+", SECRETS_JOB)
    assert re.search(r"GITLEAKS_SHA256: [0-9a-f]{64}", SECRETS_JOB)
    assert "sha256sum --check --strict" in SECRETS_JOB


def test_neither_scan_can_print_the_secret_it_found():
    """The report is world-readable build output; an unredacted hit is a leak."""
    for scan in ("gitleaks git", "gitleaks dir"):
        assert re.search(rf"{scan} \. .*--redact", SECRETS_JOB), scan


def test_the_scan_reports_are_kept_even_when_the_scan_fails():
    upload = SECRETS_JOB.split("Upload scan reports", 1)[1]
    assert "if: always()" in upload
    assert "gitleaks-history.json" in upload
    assert "gitleaks-worktree.json" in upload


def test_the_audit_runs_on_a_schedule_as_well_as_on_pushes():
    """An advisory appears without a commit, so pushes alone would not surface it."""
    assert "cron:" in SUPPLY_CHAIN


def test_the_audited_closure_is_the_one_a_user_installs():
    """Auditing this job's own environment would report risk nobody is exposed to."""
    dependencies_job = SUPPLY_CHAIN.split("dependencies:", 1)[1]
    assert "python -m venv runtime-venv" in dependencies_job
    assert "pip install --quiet ." in dependencies_job
    assert "pip freeze --exclude-editable > runtime-requirements.txt" in dependencies_job
    assert "--requirement runtime-requirements.txt" in dependencies_job


def test_the_audit_and_sbom_tools_are_pinned_to_exact_versions():
    for tool in ("pip-audit", "cyclonedx-bom"):
        assert re.search(rf"{re.escape(tool)}==\d+\.\d+\.\d+", SUPPLY_CHAIN), tool
        assert re.search(rf"{re.escape(tool)}==\d+\.\d+\.\d+", RELEASE), tool


def test_the_audit_is_informational_on_a_push_and_blocking_on_a_release():
    """Deliberately asymmetric, and the asymmetry is the whole design.

    A push should not go red because someone published an advisory overnight; a
    publish is exactly the moment that advisory should stop.
    """
    audit_step = SUPPLY_CHAIN.split("Audit the closure against the advisory database", 1)[1]
    assert "continue-on-error: true" in audit_step.split("- name:", 1)[0]
    assert "pip-audit --requirement" in RELEASE
    assert "continue-on-error" not in RELEASE


def test_the_sbom_describes_the_wheel_that_is_about_to_be_published():
    build_stage, publish_stage = RELEASE.split("publish:", 1)
    assert "pip install --quiet --no-input dist/*.whl" in build_stage
    assert "cyclonedx_py environment ./sbom-venv" in build_stage
    # Everything the SBOM step needs must already exist: it reads dist/.
    assert build_stage.index("python -m build --wheel --sdist") < build_stage.index(
        "Audit and describe what the wheel installs"
    )
    assert "sbom" not in publish_stage


def test_the_sbom_is_never_written_into_the_directory_that_gets_uploaded():
    """`dist/` is uploaded wholesale to the index, so a stray file there ships."""
    assert "--output-file sbom/" in RELEASE
    assert "--output-file dist/" not in RELEASE
    assert "name: btap-sbom" in RELEASE


def test_the_scan_output_cannot_be_committed_by_accident():
    """These land in the repository root when a maintainer reproduces a CI result."""
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (
        "*-venv/",
        "sbom/",
        "runtime-requirements.txt",
        "pip-audit.*",
        "gitleaks-*.json",
    ):
        assert f"\n{pattern}\n" in gitignore, pattern
    # A trailing comment would become part of the pattern and match nothing.
    for line in gitignore.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            assert " #" not in stripped, line


def test_local_agent_permission_state_cannot_be_committed_by_accident():
    """The one file in this tree that was untracked *and* unignored.

    `.claude/` is deliberately not ignored -- skills and commands there are part
    of the published surface -- but `settings.local.json` records one machine's
    permission decisions (absolute interpreter paths, home-directory read
    allowances, the names of that machine's other MCP servers), and the backup an
    agent tool writes when it switches permission modes was matched by no rule at
    all. `git add -A` would have published it.
    """
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (
        ".claude/settings.local.json",
        ".claude/settings.local.json.*",
        "*.bak-*",
        "*.bak",
    ):
        assert f"\n{pattern}\n" in gitignore, pattern
    # The directory itself must stay publishable, or the skills go with it.
    assert "\n.claude/\n" not in gitignore


def test_git_agrees_that_those_files_are_ignored():
    """The rules above are only worth having if git resolves them that way.

    A pattern can be present and still not match -- `.claude/settings.local.json`
    with a `!` negation later in the file, or an ordering that re-includes the
    directory. Ask git rather than reasoning about precedence.
    """
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("needs a git checkout")
    names = [
        ".claude/settings.local.json",
        ".claude/settings.local.json.bak-bypass",
        "some-config.json.bak-anything",
    ]
    completed = subprocess.run(
        ["git", "check-ignore", "--no-index", *names],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    ignored = set(completed.stdout.split())
    assert ignored == set(names), completed.stdout or completed.stderr


def test_every_action_is_pinned_to_a_commit_except_the_one_documented_exception():
    """A floating tag is a third party that can change between green runs.

    `actions/checkout@v4` is a moving pointer: whoever controls that tag decides
    what runs next time, on a runner holding this repository's checkout and, in
    release.yml, an OIDC identity that can publish to PyPI. Pinning to a commit
    is what makes a green run reproducible.

    The publish step is the deliberate exception: PyPA ships Trusted Publishing
    fixes onto `release/v1` and asks callers to track it, and a stale pin there
    fails closed on the one step that cannot be retried by hand. Listing it here
    by name is the point -- a second floating action fails this test instead of
    quietly becoming the convention.
    """
    allowed_floating = {"pypa/gh-action-pypi-publish@release/v1"}
    floating = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = re.search(r"uses:\s*(\S+)", line)
            if not match:
                continue
            ref = match.group(1)
            if ref.startswith("./") or ref in allowed_floating:
                continue
            if not re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref):
                floating.append(f"{path.name}:{line_no} {ref}")
    assert not floating, "unpinned actions: " + ", ".join(floating)


def test_the_gate_toolchain_cannot_change_under_the_gates():
    """The last third party in the gate path that was resolved fresh each run.

    Actions are pinned by commit, the secret scanner by version and digest, the
    audit and SBOM tools by exact version -- but the tools that produce every
    number in the acceptance report were whatever the index served that day. A
    ruff minor that adds a rule turns the lint gate red with no commit behind
    it, and one that drops a rule stops enforcing it just as quietly. These are
    bounds, not a hash-pinned lockfile, and the docstring is the honest limit of
    what they buy: the runtime dependencies a user installs stay unbounded on
    purpose.
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dev = pyproject.split("\ndev = [", 1)[1].split("\n]", 1)[0]
    requirements = [
        line.strip().strip('",')
        for line in dev.splitlines()
        if line.strip().startswith('"')
    ]
    assert len(requirements) >= 7, f"the dev extra stopped listing requirements: {dev}"
    unbounded = [item for item in requirements if "<" not in item]
    assert not unbounded, "no upper bound in [dev]: " + ", ".join(unbounded)


def test_the_pinned_actions_still_say_which_version_they_are():
    """A bare 40-character SHA tells a reader nothing about how old it is.

    Without the trailing version comment the only way to find out whether a pin
    is a year behind is to resolve it against the upstream repository, which is
    exactly the work nobody does. It is a comment, so it can lie -- it is here to
    give a reviewer a starting point, not to be authoritative.
    """
    unlabelled = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"uses:\s*\S+@[0-9a-f]{40}", line) and not re.search(
                r"#\s*v\d+\.\d+", line
            ):
                unlabelled.append(f"{path.name}:{line_no}")
    assert not unlabelled, "pins with no version comment: " + ", ".join(unlabelled)
