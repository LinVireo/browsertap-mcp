#!/usr/bin/env python3
"""
Packaging exclusion regression test.

Verifies that private key materials (*.pem, *.key, *.p12, *.pfx) are excluded
from both wheel and sdist distributions, even when present in the source tree.

This test uses a completely isolated synthetic fixture project that reproduces
the packaging rules from the real pyproject.toml and MANIFEST.in, avoiding any
writes to the actual source tree.

Usage:
    python scripts/test_packaging_exclusion.py

Exit code 0: all exclusions verified
Exit code 1: one or more private key patterns leaked into a distribution
"""

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from tarfile import TarFile

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[import-not-found]

SENTINEL_CONTENT = "SYNTHETIC_PRIVATE_KEY_SENTINEL_DO_NOT_PACKAGE"
PRIVATE_KEY_PATTERNS = ["*.pem", "*.key", "*.p12", "*.pfx"]


def create_isolated_fixture(work_dir: Path, project_root: Path) -> Path:
    """Create a completely isolated fixture project with synthetic private keys.

    Returns the fixture root directory. The fixture reproduces the packaging
    rules from the real project but builds in complete isolation.
    """
    fixture_root = work_dir / "fixture"
    fixture_src = fixture_root / "src" / "browsertap_mcp" / "chrome_extension"
    fixture_src.mkdir(parents=True)

    # Make it a package
    (fixture_src.parent / "__init__.py").write_text("", encoding="utf-8")

    # Create synthetic private key files
    for pattern in PRIVATE_KEY_PATTERNS:
        ext = pattern[1:]  # Remove *
        (fixture_src / f"test_private{ext}").write_text(SENTINEL_CONTENT, encoding="utf-8")

    # Create a public file that SHOULD be packaged
    (fixture_src / "manifest.json").write_text('{"name": "test"}', encoding="utf-8")

    # Read real packaging rules
    real_pyproject = (project_root / "pyproject.toml").read_bytes()
    real_manifest = (project_root / "MANIFEST.in").read_bytes()

    config = tomllib.loads(real_pyproject.decode("utf-8"))
    setuptools_config = config["tool"]["setuptools"]
    include_patterns = setuptools_config.get("package-data", {}).get("browsertap_mcp", [])
    exclude_patterns = setuptools_config.get("exclude-package-data", {}).get("browsertap_mcp", [])

    # Create minimal pyproject.toml with same packaging rules
    fixture_pyproject = (
        '[build-system]\n'
        'requires = ["setuptools>=77"]\n'
        'build-backend = "setuptools.build_meta"\n'
        '\n'
        '[project]\n'
        'name = "btap-packaging-test-fixture"\n'
        'version = "0.0.0"\n'
        '\n'
        '[tool.setuptools.packages.find]\n'
        'where = ["src"]\n'
        '\n'
        '[tool.setuptools.package-data]\n'
        'browsertap_mcp = ' + json.dumps(include_patterns) + '\n'
        '\n'
        '[tool.setuptools.exclude-package-data]\n'
        'browsertap_mcp = ' + json.dumps(exclude_patterns) + '\n'
    )
    (fixture_root / "pyproject.toml").write_text(fixture_pyproject, encoding="utf-8")
    (fixture_root / "MANIFEST.in").write_bytes(real_manifest)

    return fixture_root


def build_distributions(fixture_root: Path, work_dir: Path) -> tuple[Path, Path]:
    """Build wheel and sdist from the isolated fixture."""
    dist_dir = work_dir / "dist"
    dist_dir.mkdir()

    # Build the fixture project, not the real one
    result = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(dist_dir), str(fixture_root)],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        print(f"Build failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    wheels = list(dist_dir.glob("*.whl"))
    sdists = list(dist_dir.glob("*.tar.gz"))

    if not wheels or not sdists:
        print(f"Build produced no artifacts in {dist_dir}", file=sys.stderr)
        sys.exit(1)

    return wheels[0], sdists[0]


def check_wheel(wheel_path: Path) -> list[str]:
    """Return list of private key files found in wheel."""
    leaked = []
    with zipfile.ZipFile(wheel_path) as zf:
        for name in zf.namelist():
            if any(name.endswith(ext[1:]) for ext in PRIVATE_KEY_PATTERNS):
                # Verify it's our sentinel
                content = zf.read(name).decode("utf-8", errors="ignore")
                if SENTINEL_CONTENT in content:
                    leaked.append(name)
    return leaked


def check_sdist(sdist_path: Path) -> list[str]:
    """Return list of private key files found in sdist."""
    leaked = []
    with TarFile.open(sdist_path, "r:gz") as tf:
        for member in tf.getmembers():
            if member.isfile() and any(
                member.name.endswith(ext[1:]) for ext in PRIVATE_KEY_PATTERNS
            ):
                # Verify it's our sentinel
                extracted = tf.extractfile(member)
                if extracted:
                    content = extracted.read().decode("utf-8", errors="ignore")
                    if SENTINEL_CONTENT in content:
                        leaked.append(member.name)
    return leaked


def main():
    """Run packaging exclusion regression test."""
    project_root = Path(__file__).parent.parent

    with tempfile.TemporaryDirectory(prefix="packaging_test_") as tmpdir:
        work_dir = Path(tmpdir)

        # Create isolated fixture project
        print("Creating isolated fixture project...")
        fixture_root = create_isolated_fixture(work_dir, project_root)
        print(f"  Fixture: {fixture_root}")

        # Build distributions from the fixture
        print("\nBuilding distributions...")
        wheel_path, sdist_path = build_distributions(fixture_root, work_dir)
        print(f"  Wheel: {wheel_path.name}")
        print(f"  Sdist: {sdist_path.name}")

        # Check for leaks
        print("\nChecking distributions for private key leaks...")
        wheel_leaks = check_wheel(wheel_path)
        sdist_leaks = check_sdist(sdist_path)

        # Report
        report = {
            "wheel": {"path": str(wheel_path), "leaked": wheel_leaks},
            "sdist": {"path": str(sdist_path), "leaked": sdist_leaks},
        }

        if wheel_leaks or sdist_leaks:
            print("\n❌ PACKAGING EXCLUSION FAILED", file=sys.stderr)
            print(json.dumps(report, indent=2), file=sys.stderr)
            return 1

        print("✓ Wheel: no private keys leaked")
        print("✓ Sdist: no private keys leaked")
        print("\n✓ All packaging exclusions verified")
        return 0


if __name__ == "__main__":
    sys.exit(main())
