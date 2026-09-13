"""Exercise the actual package-data rules with synthetic private-key sentinels."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

# ruff: noqa: S101 - pytest test file requires assert statements
from browsertap_mcp.extension_build import ExtensionStampError, compute_extension_stamp
from scripts.check_distribution import _forbidden_reason, archive_names

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("filename", ["key.pem", "KEY.PEM", "private.key", "key.p12", "key.PFX"])
def test_private_key_names_are_rejected_before_reading(tmp_path, monkeypatch, filename):
    (tmp_path / filename).write_bytes(b"SYNTHETIC SENTINEL, NOT A KEY")
    monkeypatch.setattr(Path, "read_bytes", lambda _self: pytest.fail("private file was read"))
    with pytest.raises(ExtensionStampError, match="outside the extension directory"):
        compute_extension_stamp(tmp_path)
    assert _forbidden_reason(f"browsertap_mcp/chrome_extension/{filename}") == (
        "private key or certificate bundle"
    )


def test_real_wheel_and_sdist_exclude_synthetic_private_material(tmp_path):
    project = ROOT.joinpath("pyproject.toml").read_text(encoding="utf-8")
    sections = []
    for name in ("package-data", "exclude-package-data"):
        match = re.search(rf"(?ms)^\[tool\.setuptools\.{name}\]\n.*?(?=^\[|\Z)", project)
        assert match is not None
        sections.append(match[0])
    tmp_path.joinpath("pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools", "wheel"]\n'
        'build-backend = "setuptools.build_meta"\n'
        '[project]\nname = "btap-synthetic-packaging"\nversion = "0.0.0"\n'
        '[tool.setuptools]\npackage-dir = {"" = "src"}\ninclude-package-data = true\n'
        '[tool.setuptools.packages.find]\nwhere = ["src"]\n'
        + "\n".join(sections), encoding="utf-8",
    )
    tmp_path.joinpath("MANIFEST.in").write_bytes(ROOT.joinpath("MANIFEST.in").read_bytes())
    package = tmp_path / "src" / "browsertap_mcp"
    extension = package / "chrome_extension"
    extension.mkdir(parents=True)
    package.joinpath("__init__.py").write_text("", encoding="utf-8")
    extension.joinpath("public.js").write_text("// synthetic public asset\n", encoding="utf-8")
    for name in ("key.pem", "KEY.PEM", "private.key", "key.p12", "key.PFX", "config.js"):
        extension.joinpath(name).write_text("SYNTHETIC SENTINEL, NOT A KEY\n", encoding="utf-8")
    locale = extension / "_locales" / "en"
    locale.mkdir(parents=True)
    locale.joinpath("messages.json").write_text("{}", encoding="utf-8")
    locale.joinpath("nested.pem").write_text("SYNTHETIC SENTINEL, NOT A KEY", encoding="utf-8")
    node = tmp_path / "tests" / "node"
    node.mkdir(parents=True)
    for name in ("dialog_scope_harness.cjs", "native_transport_harness.cjs"):
        node.joinpath(name).write_text("module.exports = {};\n", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--wheel", "--sdist"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    archives = sorted(tmp_path.joinpath("dist").iterdir())
    assert len(archives) == 2
    for archive in archives:
        names = archive_names(archive)
        assert any(name.endswith("/chrome_extension/public.js") for name in names)
        assert not [name for name in names if _forbidden_reason(name)], names
        if archive.name.endswith(".tar.gz"):
            for harness in node.iterdir():
                assert any(name.endswith("/tests/node/" + harness.name) for name in names)
