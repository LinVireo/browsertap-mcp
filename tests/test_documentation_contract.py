from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.check_tool_docs import SKILL_ROOT, build_report, report_ok

ROOT = Path(__file__).resolve().parents[1]

# A drive letter followed by a separator: `D:\venvs\...`, `C:/Users/...`. The
# lookbehind keeps URL schemes (`http://`, `chrome://`) out of the pattern.
_LOCAL_PATH_RE = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]\S*")

# Every file that ships inside `chrome_extension/`. Spelled out rather than
# globbed, so adding one is a decision recorded here instead of an unnoticed
# arrival: anything in that directory which is not listed fails the check below.
# A new browser-side file is the event nothing else in the suite sees, and it is
# how two files once shipped for three releases with no test aware they existed.
SHIPPED_EXTENSION_FILES = frozenset(
    {
        "background.js",
        "guarded_eval.js",
        "result_serialization.js",
        "content.js",
        "disable_dialogs.js",
        "manifest.json",
        "popup.html",
        "popup.js",
        "_locales/en/messages.json",
        "_locales/zh_CN/messages.json",
        # Drawn here for the Chrome Web Store listing, which requires a 128x128
        # icon inside the package. The generator that produced them is not part
        # of the distribution.
        "icon16.png",
        "icon32.png",
        "icon48.png",
        "icon128.png",
    }
)


def _shipped_skills() -> list[Path]:
    """The agent skills as the release actually carries them.

    Discovered rather than listed: they are package data now, so a skill added
    later is covered by every check below without editing this file.
    """
    return sorted(SKILL_ROOT.glob("*/SKILL.md"))


def test_tool_docs_and_caller_skill_are_synchronized():
    # No skip: the skills ship inside the package, so an absent one is a
    # packaging regression that would leave `browsertap skill-path`
    # pointing at an empty directory.
    assert _shipped_skills(), f"no <name>/SKILL.md under {SKILL_ROOT}"
    report = build_report()
    assert report_ok(report), report


def test_documentation_contract_covers_all_registered_tools():
    report = build_report()
    assert report["registered"] == 51
    assert report["coverage_manifest"] == 51
    assert not report["readme_missing"]
    assert not report["readme_extra"]
    assert not report["missing_params"]
    assert not report["missing_defaults"]


def test_public_guides_cover_install_diagnostics_and_security_boundaries():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    troubleshooting = (ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    contributing_zh = (ROOT / "CONTRIBUTING.zh-CN.md").read_text(encoding="utf-8")

    for text in (readme, readme_zh):
        assert "git clone https://github.com/LinVireo/browsertap-mcp.git" in text
        assert ".venv\\Scripts\\browsertap.exe" in text
        assert "BROWSERTAP_WS_ALLOWED_ORIGINS" in text
        assert "BROWSERTAP_WS_ALLOW_NO_ORIGIN" in text
        assert "registering" in text
        assert "bridge_error" in text
        assert "switched_session" in text
        assert "browsertap bridge --stop" in text
        assert "pip uninstall browsertap-mcp" in text
        assert "chrome://extensions" in text
    assert "no_response" in troubleshooting
    assert "BROWSERTAP_BRIDGE_PORT" in troubleshooting
    # The bridge port is configured on the Python side and mirrored into the
    # extension from its service-worker console. It is deliberately NOT an
    # editable field in the popup, so the guide must carry the console step.
    assert "chrome.storage.local.set({ btap_port" in troubleshooting

    assert "declarativeNetRequest" in security
    assert "three business days" in security
    assert "python -m scripts.lint_report" in contributing
    assert "python -m scripts.lint_report" in contributing_zh
    assert "--cov-fail-under=95" in contributing
    assert "release/*" in contributing
    assert "[简体中文](CONTRIBUTING.zh-CN.md)" in contributing
    assert "[English](CONTRIBUTING.md)" in contributing_zh
    assert "python -m scripts.finalize_change --bump none --skip-live" in contributing_zh
    for text in (contributing, contributing_zh):
        # `check_distribution` inspects the archives `build` writes, so a guide
        # that lists it without the build step sends the reader to
        # `no wheel found` and calls it a gate run.
        assert "python -m build --wheel --sdist --outdir artifacts/dist" in text
        assert text.index("python -m build --wheel --sdist") < text.index(
            "python -m scripts.check_distribution artifacts/dist"
        )
        # The tool-evidence report is a gate in both CI and the finalizer.
        assert "python -m scripts.tool_coverage_report --format markdown" in text
        # `ruff format` is not a gate and most sources are not format-clean;
        # telling contributors to run it produces unrelated reflow diffs.
        assert "ruff format --check path/to/changed.py" not in text
        # The install gate sits in the same block as the build, and locally it is
        # the layout-only variant: a guide that omits that flag sends a reader
        # with no index access into a failure that proves nothing about the tree.
        assert "python -m scripts.check_install artifacts/dist --no-deps" in text
        assert text.index("python -m scripts.check_distribution artifacts/dist") < text.index(
            "python -m scripts.check_install artifacts/dist --no-deps"
        )
        # Both guides carry the tag check and both halves of the secret scan, or
        # one language ships a weaker release procedure than the other.
        assert "python -m scripts.check_release_tag --allow-missing-tag" in text
        assert "gitleaks git . --no-banner --redact" in text
        assert "gitleaks dir . --no-banner --redact" in text
        # The live preconditions are enforced by a fixture now, so both guides
        # have to name the artifact and stop telling readers to check the tab
        # inventory by hand -- a guide that still asks for the manual step is a
        # guide that says the automated one does not exist.
        assert "tests/live_preflight.py" in text
        assert "artifacts/live-preflight.json" in text
        # And both have to say which claim the fixture actually makes. It fails a
        # run only over a tab the suite opened itself; the user's tabs are theirs
        # to open and close mid-run. A guide still promising "the inventory comes
        # out the way it went in" sends the reader to blame the wrong thing, and
        # the env var that used to downgrade that check no longer exists.
        assert "BTAP_LIVE_ALLOW_BUSY_BROWSER" not in text
        assert "_TAB_OWNERSHIP.outstanding()" in text
        assert "lifecycle generation changed" in text
        # `enforced` is the field that separates "nothing leaked" from "nothing
        # was opened, so nothing was measured".
        assert "own_tabs.enforced" in text
        # The third precondition is the one a reader cannot infer: two of the
        # three processes are long-lived, so a live pass can be a pass for code
        # that is not in the tree. A guide that omits it leaves the reader with
        # the impression that running the suite is enough to have tested the
        # checkout, which is exactly the belief that let a stale extension
        # through a whole release round.
        assert "get_setup_status()" in text


def test_the_readmes_open_with_a_three_step_start():
    """The first screen has to hand the reader a command, not a feature list.

    Checked per language rather than in the shared loop above: a first-screen
    block is exactly the kind of edit that lands in one README and is forgotten
    in the other, and the shared loop cannot tell which text it is holding.
    """
    for name, heading, features in (
        ("README.md", "## Start in 60 seconds", "## Key features"),
        ("README.zh-CN.md", u"## 60 秒上手", u"## 核心能力"),
    ):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert heading in text, name
        # Above the feature list, or it is not on the first screen any more.
        assert text.index(heading) < text.index(features), name
        block = text.split(heading, 1)[1].split(features, 1)[0]

        # All three steps, in the one place a stranger will actually read.
        # The literal is the *published* install, not the editable one it used to
        # be: since 0.4.12 the package is on PyPI, and a first screen that opens
        # with `git clone` tells a reader who only wants to use the server to do
        # work they do not need. The editable install still has its own place
        # under Getting started, for people changing the project.
        assert 'pip install "browsertap-mcp[desktop]"' in block, name
        assert "browsertap extension-path" in block, name
        assert "claude mcp add browsertap" in block, name
        # The step that cannot be scripted has to be named as manual here; it is
        # the whole reason the other two being one-liners is not the full story.
        assert "chrome://extensions" in block, name
        assert ("Load unpacked" in block) or (
            u"加载已解压的扩展程序" in block
        ), name
        # And the first prompt returning nothing is the most likely outcome of a
        # 60-second install, so the diagnostic belongs in the block, not 500
        # lines down under Troubleshooting.
        assert "browsertap doctor" in block, name


def test_public_docs_preserve_background_and_coordinate_semantics():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    assert "each omitted offset axis uses the element centre" in readme
    assert "measured from the element's top-left corner" in readme
    assert "未提供 offset 的轴取元素中心" in readme_zh
    assert "从元素左上角" in readme_zh
    assert "Windows" in readme_zh
    assert "最小化窗口" in readme_zh
    assert "on_screen=false" in readme_zh


def test_troubleshooting_quotes_the_literals_the_code_actually_emits():
    """A guide that paraphrases an error message cannot be searched for.

    These three strings are what a caller sees verbatim: the plain-text 401
    body, the refusal that fires when a named tab is gone, and the field that
    says an unnamed target was re-picked. All three previously existed only in
    the source, so the operator hitting one had nothing to look up.
    """
    bridge = (ROOT / "src" / "browsertap_mcp" / "browser_bridge.py").read_text(
        encoding="utf-8"
    )
    # Adjacent string fragments are one value at runtime, so collapse the source
    # line breaks between them before searching for what the caller receives.
    emitted = re.sub(r'"\s*\n\s*f?"', "", bridge)
    guides = [
        (ROOT / "docs" / "TROUBLESHOOTING.md").read_text(encoding="utf-8"),
        (ROOT / "docs" / "TROUBLESHOOTING.zh-CN.md").read_text(encoding="utf-8"),
    ]

    literals = (
        "unauthorized: missing or bad bridge token",
        "is not connected. BTAP refused to execute on a different tab",
        "switched_session",
    )
    for literal in literals:
        assert literal in emitted, f"{literal!r} is no longer emitted by the bridge"
        for guide in guides:
            assert literal in guide
    for guide in guides:
        # The 401 body is not JSON. A client that assumes it is reports a parse
        # error instead of the missing token, so both guides have to say so.
        assert "JSON" in guide
        assert "switched_from" in guide


def test_public_docs_do_not_promise_a_quiet_gate_the_os_may_not_afford():
    """The gate's own limits have to reach the reader who trusts it.

    Every published description of the physical-input sequence used to say the
    quiet window cancels the action on user activity, full stop. It can only do
    that where the OS answers: the last-input timestamp is Windows-only and the
    pointer probe is blind under Wayland, in a headless container, and on macOS
    without the accessibility permission. So the field the code now emits has to
    be findable in the guides -- a reader who sees `enforced: false` and cannot
    look it up is back to assuming the desktop was idle, which is the belief the
    fix exists to remove.
    """
    physical = (ROOT / "src" / "browsertap_mcp" / "physical_input.py").read_text(
        encoding="utf-8"
    )
    # Proof the docs are describing something real, in both directions: the field
    # is emitted, and `enforced` is computed rather than hardcoded to True.
    assert '"input_quiet"' in physical
    assert '"enforced": bool(observed)' in physical

    guides = {
        "README.md": ROOT / "README.md",
        "README.zh-CN.md": ROOT / "README.zh-CN.md",
        "docs/USAGE.md": ROOT / "docs" / "USAGE.md",
        "docs/USAGE.zh-CN.md": ROOT / "docs" / "USAGE.zh-CN.md",
        "docs/TROUBLESHOOTING.md": ROOT / "docs" / "TROUBLESHOOTING.md",
        "docs/TROUBLESHOOTING.zh-CN.md": ROOT / "docs" / "TROUBLESHOOTING.zh-CN.md",
    }
    for name, path in guides.items():
        text = path.read_text(encoding="utf-8")
        assert "input_quiet" in text, f"{name} describes the gate without its report"

    # The troubleshooting guides are where someone lands with the value in hand,
    # so they carry the reason as well as the field, in both languages.
    english = guides["docs/TROUBLESHOOTING.md"].read_text(encoding="utf-8")
    chinese = guides["docs/TROUBLESHOOTING.zh-CN.md"].read_text(encoding="utf-8")
    assert "input_quiet.enforced: false" in english
    # These guides are hard-wrapped, so search for words rather than phrases:
    # "accessibility permission" straddles a line break today and re-wrapping a
    # paragraph must not read as the caveat having been deleted.
    assert "Wayland" in english and "accessibility" in english
    assert "unverified" in english
    assert "`input_quiet.enforced`" in chinese
    assert "Wayland" in chinese
    assert "未经验证" in chinese


def test_public_maintenance_commands_use_module_invocation():
    # Only documents that ship. `docs/superpowers/` was untracked and
    # gitignored in 0.3.14, so naming a file there made this contract
    # unverifiable on a fresh clone: it passed on the maintainer machine,
    # where the untracked file still exists, and raised FileNotFoundError
    # anywhere else.
    paths = (
        ROOT / "CONTRIBUTING.md",
        ROOT / "CONTRIBUTING.zh-CN.md",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "python scripts/" not in text
        assert "python scripts\\" not in text


def test_canonical_caller_skill_is_not_machine_specific():
    paths = _shipped_skills()
    assert paths, f"no <name>/SKILL.md under {SKILL_ROOT}"

    for path in paths:
        skill = path.read_text(encoding="utf-8")
        label = f"{path.parent.name}/{path.name}"

        for local_claim in ("本机已移除", "用户本机", "本机一人使用", "本机默认"):
            assert local_claim not in skill, f"{label} states a machine-local fact"
        # These skills are written from the maintainer's machine, where the
        # absolute paths happen to work. A reader on any other machine follows
        # them into a directory that does not exist.
        drive_paths = sorted(set(_LOCAL_PATH_RE.findall(skill)))
        assert not drive_paths, f"{label} carries absolute local path(s) {drive_paths}"


def test_published_agent_guide_is_not_machine_specific():
    """`AGENTS.md` ships; `AGENTS.local.md` is where machine detail belongs.

    The published guide grew out of one maintainer's notebook and carried
    absolute interpreter paths plus the layout of that machine's skill manager.
    A reader who had just cloned the repository followed those into directories
    that do not exist, and the layout went stale as soon as the machine was
    reorganised. The split only holds while the ignore rule keeps the local half
    untracked, so that is asserted here too rather than trusted.
    """
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    found = sorted(set(_LOCAL_PATH_RE.findall(text)))
    assert not found, f"AGENTS.md carries absolute local path(s) {found}"

    rules = {line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert "AGENTS.local.md" in rules
    assert "AGENTS.md" not in rules, "AGENTS.md is meant to ship, not to be ignored"
    assert "AGENTS.local.md" in text, "AGENTS.md has to say where machine-local notes go"

    # The guide tells a contributor which files to edit alongside a tool change.
    # Naming them by path means a moved skill has to break here loudly instead of
    # sending the next reader to a path that no longer exists.
    for path in _shipped_skills():
        reference = path.relative_to(ROOT).as_posix()
        assert reference in text, f"AGENTS.md does not point at {reference}"



def test_readme_links_survive_being_read_off_the_repository():
    """README.md is the package's long description on the index page.

    An index renders it standalone, so a relative link there resolves against
    the index host and 404s -- the reader is one click from the usage guide, the
    security policy and the licence, and gets none of them. Absolute links work
    from both places, so the READMEs pay that cost and the rest of the docs,
    which are only ever read inside the tree, keep relative links.
    """
    repository = "https://github.com/LinVireo/browsertap-mcp/"
    for name in ("README.md", "README.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        targets = re.findall(r"\]\(([^)\s]+)\)", text)
        assert targets, f"{name}: the link scan stopped matching"
        relative = [
            target
            for target in targets
            if not target.startswith(("https://", "http://", "#", "mailto:"))
        ]
        assert not relative, f"{name} links to {relative} relatively, which breaks off-tree"
        # An absolute link is only useful if it points at this repository; one
        # left pointing at some other repository sends the reader to code that
        # is not this project's.
        in_repo = [target for target in targets if target.startswith(repository)]
        assert in_repo, f"{name} no longer links into {repository}"
        for target in in_repo:
            tail = target[len(repository):]
            if not tail.startswith("blob/main/"):
                continue
            referenced = ROOT / tail[len("blob/main/"):]
            assert referenced.exists(), f"{name} links to a missing file: {tail}"


def test_user_facing_docs_carry_no_pre_unification_version_numbers():
    """The extension used to version itself separately (2.x).

    Everything is unified on the package version now, so a surviving `2.x.y` in
    a user-facing doc tells the reader the advice applies to a build that no
    longer exists. Dependency versions belong in CHANGELOG or CONTRIBUTING, not
    in these files, so any `2.x.y` here is a leftover BTAP version.
    """
    stale = re.compile(r"(?<![\w.])2\.\d+\.\d+(?![\w.])")
    paths = [
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
        ROOT / "docs" / "USAGE.md",
        ROOT / "docs" / "USAGE.zh-CN.md",
        ROOT / "docs" / "TROUBLESHOOTING.md",
        ROOT / "docs" / "TROUBLESHOOTING.zh-CN.md",
    ]
    # The agent skills are the most likely place for a stale extension version
    # to survive, because they tell a reader which build to compare against.
    paths += _shipped_skills()
    for path in paths:
        if not path.is_file():
            continue
        found = sorted(set(stale.findall(path.read_text(encoding="utf-8"))))
        name = path.relative_to(ROOT).as_posix()
        assert not found, f"{name} still references pre-unification version(s) {found}"


def test_example_client_names_match_the_standard_config():
    for name in ("claude-desktop-config.json", "cursor-mcp.json"):
        payload = json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))
        assert payload == {
            "mcpServers": {
                "browsertap": {
                    "type": "stdio",
                    "command": "browsertap",
                    "args": [],
                }
            }
        }
    hermes = (ROOT / "examples" / "hermes-config.yaml").read_text(encoding="utf-8")
    assert "  browsertap:\n" in hermes
    assert "browsertap_mcp" not in hermes


def test_the_registry_listing_and_the_readme_claim_the_same_namespace():
    """The pair is what proves the namespace belongs to this repository.

    An MCP Registry submission is accepted only if `server.json`'s `name` and the
    `mcp-name` marker in the published README agree: the marker is the proof of
    ownership and the manifest is the claim. The marker has been in README.md
    since 0.3.12 with no manifest beside it, so the two can drift the moment one
    is edited -- and the failure surfaces at submission time, in a registry, with
    nothing in the repository having complained.
    """
    marker = re.search(
        r"^<!--\s*mcp-name:\s*(\S+)\s*-->$",
        (ROOT / "README.md").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert marker is not None, "README.md must carry the mcp-name ownership marker"

    listing = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert listing["name"] == marker.group(1)
    # The namespace half has to be the repository owner, or the registry has no
    # way to tie the marker to anyone.
    assert listing["name"].startswith("io.github.")
    # Read both halves off the repository URL rather than the checkout: a clone
    # into a differently named directory is not a listing defect.
    owner, repository = listing["repository"]["url"].rsplit("/", 2)[1:]
    assert listing["name"] == f"io.github.{owner}/{repository}"
    # `description` is what a stranger reads in the registry listing, and the
    # schema caps it at 100 characters.
    assert 0 < len(listing["description"]) <= 100
    assert listing["packages"][0]["transport"] == {"type": "stdio"}


def test_prose_tool_counts_track_the_registered_total():
    """Five documents state the tool count in prose, and nothing checked it.

    The count is pinned twice in `check_tool_docs` against a constant, so adding a
    tool means editing that constant -- and at that moment every sentence saying
    "56 tools" becomes wrong with no gate between it and a reader. These are the
    lines a stranger uses to decide whether the table they are reading is the
    whole contract.

    The pattern is deliberately narrow: two digits or more, and not "3 tool
    calls", so ordinary prose about a handful of tools does not turn this red.
    Subset counts in these documents are spelled out in words.
    """
    report = build_report()
    total = report["registered"]
    pattern = re.compile(r"(\d{2,})[-\s]+(?:tools?\b(?!\s+calls?\b)|个工具)")

    paths = [
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
        ROOT / "AGENTS.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "CONTRIBUTING.zh-CN.md",
        ROOT / "docs" / "USAGE.md",
        ROOT / "docs" / "USAGE.zh-CN.md",
        ROOT / "docs" / "TROUBLESHOOTING.md",
        ROOT / "docs" / "TROUBLESHOOTING.zh-CN.md",
    ]
    paths += _shipped_skills()

    found = []
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        name = path.relative_to(ROOT).as_posix()
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{name}:{line}")
            assert int(match.group(1)) == total, (
                f"{name}:{line} says {match.group(1)} tools, but {total} are registered"
            )

    # A scan that matched nothing would pass while measuring nothing. If the
    # prose counts were removed on purpose, remove this test with them.
    assert found, "no document states the tool count any more; this check is now vacuous"


def test_the_registry_listing_describes_environment_variables_that_exist(monkeypatch):
    """The listing is the one surface no gate could reach from the repository.

    `server.json` shipped saying `BROWSERTAP_MODE` took `strict, standard or lab`
    and defaulted to `standard`. Two of those three names do not exist
    (`server._AUTOMATION_MODES` is `lab`/`safe`) and the real default is `lab`, so
    a stranger following the registry would set `standard`, be silently folded
    back to `lab` by `_automation_mode`, and believe they had asked for approval
    prompts on a profile that skips them and drives real mouse and keyboard input.
    Every other gate passed: the namespace, the version copies, the identifier and
    the schema were all correct, because none of them reads this block.

    Once a listing is accepted it is invisible from here -- the repository is not
    what a client reads -- so the description has to be pinned to the code while
    both are still in one tree.
    """
    from browsertap_mcp import server as btap_server

    listing = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    declared = [
        entry
        for package in listing["packages"]
        for entry in package.get("environmentVariables", [])
    ]
    assert declared, "the listing must describe how to configure the server"

    # Every name has to be one the code actually reads. A renamed or invented
    # variable is documentation for a knob that does not turn.
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "src" / "browsertap_mcp").rglob("*.py"))
    )
    for entry in declared:
        assert entry["name"] in sources, (
            f"server.json documents {entry['name']}, which no module reads"
        )
        assert entry["description"].strip(), f"{entry['name']} has no description"

    # `BROWSERTAP_MODE` is pinned further than the rest because it is the one
    # whose wrong answer grants privilege rather than merely failing.
    mode = next(e for e in declared if e["name"] == "BROWSERTAP_MODE")
    stated = re.match(
        r"Approval profile: (?P<values>[a-z]+(?:(?:, | or )[a-z]+)*)\."
        r" Defaults to (?P<default>[a-z]+)[.,]",
        mode["description"],
    )
    assert stated is not None, (
        "the BROWSERTAP_MODE description must stay in the form "
        "'Approval profile: <values>. Defaults to <value>.' so it can be checked: "
        f"{mode['description']!r}"
    )
    values = re.split(r", | or ", stated.group("values"))
    assert set(values) == set(btap_server._AUTOMATION_MODES), (
        f"listing offers {sorted(values)}, code accepts "
        f"{sorted(btap_server._AUTOMATION_MODES)}"
    )
    # Read the default off the resolver rather than a literal: it is the value an
    # unset environment actually produces. Both inputs are cleared first, because
    # an ambient `BROWSERTAP_MODE` or a leftover override from `set_automation_
    # profile` would otherwise decide what this assertion means.
    monkeypatch.delenv("BROWSERTAP_MODE", raising=False)
    monkeypatch.setattr(btap_server, "_AUTOMATION_MODE_OVERRIDE", None)
    assert stated.group("default") == btap_server._automation_mode()


def test_the_licence_travels_with_every_copy():
    """MIT puts the obligation on the copy, not on the repository.

    The terms have to be included in "all copies or substantial portions", and
    the copy most people receive is the wheel -- not this repository. So the
    licence being present in the tree proves nothing on its own; what matters is
    that `license-files` and the distribution gate carry it into the artifact,
    which is what this asserts.
    """
    licence = ROOT / "LICENSE"
    assert licence.exists(), "LICENSE is gone; the distribution ships no terms at all"
    body = licence.read_text(encoding="utf-8")

    # The real MIT grant, not a paraphrase. Both sentences that carry the actual
    # obligation, so a truncated or reworded licence fails here.
    assert "MIT License" in body
    assert "Permission is hereby granted, free of charge" in body
    assert "The above copyright notice and this permission notice shall be included in all" in body
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in body
    assert re.search(r"^Copyright \(c\) \d{4} \S+", body, re.MULTILINE), (
        "LICENSE carries no copyright line, which is the one field MIT requires be filled in"
    )

    # Reaching the artifact is the whole point. `tomllib` is 3.11+ and this
    # package still supports 3.10, where the `dev` extra pins `tomli` for exactly
    # this -- the same fallback `scripts/versioning.py` uses.
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI
        import tomli as tomllib

    with (ROOT / "pyproject.toml").open("rb") as handle:
        metadata = tomllib.load(handle)
    declared = metadata["project"]["license-files"]
    assert "LICENSE" in declared, f"license-files dropped LICENSE: {declared}"

    from scripts.check_distribution import (
        REQUIRED_SDIST_SUFFIXES,
        REQUIRED_WHEEL_METADATA_SUFFIXES,
    )

    assert "/licenses/LICENSE" in REQUIRED_WHEEL_METADATA_SUFFIXES
    assert "/LICENSE" in REQUIRED_SDIST_SUFFIXES


def test_the_privacy_policy_discloses_every_permission_that_reads_user_data():
    """`PRIVACY.md` has to cover what `manifest.json` actually asks for.

    The Chrome Web Store's stated penalty for a policy that is narrower than the
    listing's data-usage answers is removal, not a review comment, and the
    listing's answers follow the manifest. So the failure mode worth catching is
    a permission added to the manifest without a corresponding line here -- the
    policy keeps reading as complete, because nothing in it became false.

    `alarms` and `storage` are excluded deliberately: neither reads anything
    about the user. The rest each map to a phrase the policy has to keep.
    """
    policy = ROOT / "PRIVACY.md"
    assert policy.exists(), "PRIVACY.md is gone; the Web Store listing's policy URL 404s"
    text = policy.read_text(encoding="utf-8")

    manifest = json.loads(
        (ROOT / "src" / "browsertap_mcp" / "chrome_extension" / "manifest.json")
        .read_text(encoding="utf-8")
    )
    granted = set(manifest["permissions"])
    # Housekeeping only: `alarms` schedules the reconnect timer, `storage` is the
    # mechanism the policy's own storage section describes rather than a source.
    disclosable = granted - {"alarms", "storage"}
    required_phrase = {
        "cookies": "Cookies and site storage",
        "tabs": "Open tabs",
        "debugger": "Console and network activity",
        "scripting": "Page content",
        "contentSettings": "Site permissions",
        "declarativeNetRequest": "Content-Security-Policy",
        "management": "installed extensions",
        "bookmarks": "Bookmarks",
        "downloads": "downloads",
    }
    undocumented = sorted(disclosable - set(required_phrase))
    assert not undocumented, (
        f"manifest grants {undocumented} with no phrase mapped here; add the "
        "disclosure to PRIVACY.md and the mapping to this test"
    )
    for permission in sorted(disclosable):
        phrase = required_phrase[permission]
        assert phrase in text, f"PRIVACY.md stopped disclosing {permission!r} (looked for {phrase!r})"

    # `<all_urls>` is the breadth question a reviewer asks first, and the honest
    # answer is that access is held continuously and used per command.
    assert "<all_urls>" in manifest.get("host_permissions", [])
    assert "all sites" in text and "<all_urls>" in text, (
        "PRIVACY.md must say the extension holds all-sites access, not per-site"
    )

    # Three claims that would each be a false statement if the code changed
    # under them. The loopback one is the policy's headline promise.
    assert "127.0.0.1" in text
    assert "60 to 600 seconds" in text, "the lease window in PRIVACY.md drifted from the tool"
    for name in ("README.md", "README.zh-CN.md"):
        readme = (ROOT / name).read_text(encoding="utf-8")
        assert "PRIVACY.md" in readme, f"{name} dropped the privacy policy link"


def test_no_browser_side_file_ships_without_the_suite_knowing():
    """A new file under `chrome_extension/` has to be declared here.

    That directory is the one place a shipped file can arrive with nothing else
    in the suite aware of it: it is copied wholesale into the wheel by a
    `recursive-include`, Chrome loads whatever the manifest references, and no
    Python import would fail if a stray file appeared. Two files once shipped for
    three releases with no test able to see them.

    So the list is explicit and this compares it to the directory in both
    directions -- an undeclared file fails, and so does a declared one that was
    deleted or renamed without updating the list.
    """
    extension = ROOT / "src" / "browsertap_mcp" / "chrome_extension"
    assert extension.is_dir(), "the packaged extension directory is gone"

    present = {
        path.relative_to(extension).as_posix()
        for path in extension.rglob("*")
        if path.is_file()
    }
    assert present == set(SHIPPED_EXTENSION_FILES), (
        "the extension directory and SHIPPED_EXTENSION_FILES disagree. "
        f"present but undeclared: {sorted(present - set(SHIPPED_EXTENSION_FILES))}; "
        f"declared but absent: {sorted(set(SHIPPED_EXTENSION_FILES) - present)}"
    )


def test_no_published_document_repeats_a_section():
    """A section pasted twice is invisible to every other check in this file.

    `CONTRIBUTING.md` carried two verbatim copies of "Listing on the MCP
    Registry" -- 26 lines each -- through several releases. Every fact in it was
    correct, which is why nothing caught it: the checks here look for text that
    is missing or stale, never for text that is present twice.
    """
    paths = [
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "CONTRIBUTING.zh-CN.md",
        ROOT / "AGENTS.md",
        ROOT / "SECURITY.md",
        # Both are published surfaces with the same drift exposure as the rest.
        # `PRIVACY.md` is the exact URL the Chrome Web Store listing serves as its
        # privacy policy, so a section pasted twice there is public.
        ROOT / "PRIVACY.md",
        ROOT / "CODE_OF_CONDUCT.md",
        ROOT / "docs" / "USAGE.md",
        ROOT / "docs" / "USAGE.zh-CN.md",
        ROOT / "docs" / "TROUBLESHOOTING.md",
        ROOT / "docs" / "TROUBLESHOOTING.zh-CN.md",
    ]
    checked = 0
    for path in paths:
        if not path.is_file():
            continue
        checked += 1
        headings = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("## ")
        ]
        repeated = sorted({name for name in headings if headings.count(name) > 1})
        assert not repeated, f"{path.relative_to(ROOT).as_posix()} repeats section(s) {repeated}"
    # Computed from the roster rather than a literal floor. The literal was 9
    # against a list of 11, so it kept passing while two documents were
    # unreachable -- and removing an entry from the list widened that gap
    # silently. Every path named here has to be readable or this is not checking
    # what it claims.
    assert checked == len(paths), (
        f"only {checked} of {len(paths)} documents were reachable; this check is going vacuous"
    )

# --- scan_page has to leave the user's page as it found it --------------------
#
# Until 0.4.15 it did not. With `cutlist` on -- the default -- an ordinary read of
# a page the user was looking at wrote a `data-btap-list` attribute onto the
# container it collapsed and kept two `window.__btap*` counters, because the
# selector it reported had to survive a second roundtrip. The page analysis now
# derives that selector from the container's own structure, so there is nothing
# left to mark and the tool is a read again.
#
# "It writes nothing" is a stronger claim than the disclosure it replaces, and a
# stronger claim needs a check that can falsify it. A single `el.setAttribute()`
# on a live node reinstates the old behaviour with every other test here still
# green, and three published documents would go on promising a read-only scan. So
# the two injected scripts are read on both axes a write can take: a global, and a
# node.

# `window.x = ...`, `window.x++`, `delete window.x`, and the `globalThis` spelling
# of each. Reading a global is not writing one, which is why `window.CSS &&
# CSS.escape` in `list_groups.js` is deliberately not a match.
_GLOBAL_WRITE_RE = re.compile(
    r"\b(?:window|globalThis)\.([A-Za-z_$][\w$]*)\s*(?:=[^=]|\+\+|--)"
    r"|\bdelete\s+(?:window|globalThis)\.([A-Za-z_$][\w$]*)"
)

# Every way a node can be changed. Removal is in here too: taking something out of
# the user's page is as much a write as putting something in.
_DOM_WRITE_RE = re.compile(
    r"\b([A-Za-z_$][\w$]*)\.(?:setAttribute|setAttributeNS|removeAttribute|"
    r"classList|insertAdjacentHTML|insertAdjacentElement|appendChild|insertBefore|"
    r"replaceChild|removeChild|replaceWith|append|prepend|remove)\s*\("
    r"|\b([A-Za-z_$][\w$]*)\.(?:innerHTML|outerHTML|textContent|innerText|"
    r"className|id|value|checked|src|href|style)\s*=[^=]"
)

# What makes a receiver clone-side. The allow-list is not a list of names -- a name
# is a convention, and conventions are what this file exists to stop trusting. Each
# receiver has to be *declared* in the same file from something that produces a new
# node, so `const clone = src.cloneNode(false)` passes while a receiver fetched with
# `querySelector` has no such declaration and fails.
_CLONE_DECL_RE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=[^;]*?"
    r"(?:cloneNode|createElement|createTextNode|createDocumentFragment|\bcopy\()"
)

# Writes the docs used to disclose. Naming one now is a promise the code does not
# keep -- the same failure the disclosure check this replaced was built to catch,
# pointing the other way.
_RETIRED_PAGE_WRITES = ("data-btap-list", "__btapListMark", "__btap_offscreen")


def test_scan_page_does_not_write_to_the_users_page():
    """Fail when the page analysis starts modifying the page again.

    This deliberately runs while the `.md` files are being reorganised. A
    reorganisation that drops the read-only sentence, or that leaves the retired
    `data-btap-list` disclosure standing, is a published document disagreeing with
    the code, and neither direction has a reader who would notice.
    """
    # The scan_page payload is these two files -- `simphtml._load_page_script`
    # reads them at import and the call sites append `return pageOutline(...)` /
    # `return listGroups(...)`.
    page_scripts = ROOT / "src" / "browsertap_mcp" / "page_scripts"
    scanned = ("page_outline.js", "list_groups.js")
    # A third injected script would otherwise be silently out of scope, and what it
    # writes to the user's page would reach them with nothing in the offline suite
    # looking at it. Adding one has to be a decision made here.
    present = sorted(path.name for path in page_scripts.glob("*.js"))
    assert present == sorted(scanned), (
        f"page_scripts holds {present}, but this check scans {sorted(scanned)}. "
        "A new injected script has to be added to this list (and to "
        "REQUIRED_WHEEL_SUFFIXES in scripts/check_distribution.py) in the same "
        "change, or what it writes to the user's page goes unchecked"
    )

    for name in scanned:
        source = (page_scripts / name).read_text(encoding="utf-8")

        globals_written = {
            first or second
            for first, second in _GLOBAL_WRITE_RE.findall(source)
            if first or second
        }
        assert not globals_written, (
            f"{name} assigns {sorted(globals_written)} on the page's global object. "
            "scan_page is documented as leaving the page untouched, so a global it "
            "needs is a design change rather than an implementation detail: either "
            "carry the value out in the returned payload the way the "
            "`<!--btap-offscreen:-->` marker does, or change the disclosure in both "
            "READMEs and the tool description in the same commit"
        )

        receivers = {
            first or second
            for first, second in _DOM_WRITE_RE.findall(source)
            if first or second
        }
        clone_side = set(_CLONE_DECL_RE.findall(source))
        live = sorted(receivers - clone_side)
        assert not live, (
            f"{name} mutates {live}, which that file never declares from "
            "cloneNode/createElement, so it is a node out of the user's live "
            f"document. Write to the clone instead ({sorted(clone_side)} are the "
            "receivers this file established), or change the disclosure in both "
            "READMEs and the tool description in the same commit"
        )

    # Not vacuous: the analysis does build a clone and write form state onto it, so
    # a rewrite that stops matching `_DOM_WRITE_RE` altogether has not shown the
    # scripts to be read-only -- it has stopped asking.
    outline = (page_scripts / "page_outline.js").read_text(encoding="utf-8")
    assert _DOM_WRITE_RE.search(outline), (
        "no DOM write found anywhere in page_outline.js -- the clone it returns is "
        "assembled by `setAttribute`/`appendChild`, so this check has lost its grip "
        "on the file rather than found it clean"
    )

    # The one page-visible side effect that remains, and the only reason it is
    # survivable: the console stubs are restored unconditionally. An override with
    # no restore would outlive the call and change what the page's own scripts see.
    assert "console.log = console.warn" in outline and "} finally {" in outline, (
        "page_outline.js silences the console so the analysis cannot pollute an "
        "active capture; that override has to be restored in a `finally`, or a scan "
        "leaves the page's console replaced"
    )
    assert "console.log = saved.log" in outline, (
        "page_outline.js no longer restores console.log from the saved original"
    )

    documents = {
        "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
        "README.zh-CN.md": (ROOT / "README.zh-CN.md").read_text(encoding="utf-8"),
        # Adjacent string literals are joined first. The tool description is built by
        # concatenation, so a phrase in it straddles a `" "` boundary and a literal
        # search would report the sentence missing after an ordinary reflow.
        "server.py": re.sub(
            r'"\s*"',
            "",
            (ROOT / "src" / "browsertap_mcp" / "server.py").read_text(encoding="utf-8"),
        ),
    }
    for name, text in documents.items():
        stale = [write for write in _RETIRED_PAGE_WRITES if write in text]
        assert not stale, (
            f"{name} still discloses {stale}, which scan_page no longer writes. A "
            "disclosure that outlives the behaviour sends the reader looking in "
            "their own DOM for something that is not there"
        )
    for name in ("README.md", "server.py"):
        assert "built-in scan does not write page attributes" in documents[name]
        assert "extra_js" in documents[name]
        assert "modify the page or send requests" in documents[name]
    # The Chinese table says the same thing in Chinese; sharing the English phrase
    # would only prove that someone pasted one in.
    assert "内置扫描不写页面属性" in documents["README.zh-CN.md"]
    assert "`extra_js` 执行调用方代码，可以修改页面或发送请求" in documents["README.zh-CN.md"]
