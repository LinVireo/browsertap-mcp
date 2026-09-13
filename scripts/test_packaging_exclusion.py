#!/usr/bin/env python3
"""
Packaging exclusion regression test.

Verifies that private key materials (*.pem, *.key, *.p12, *.pfx) are excluded
from both wheel and sdist distributions, even when present in the source tree.

This test uses synthetic sentinel files to avoid reading or packaging real
private keys. It independently reproduces the packaging rules from pyproject.toml
and MANIFEST.in to verify exclusion behavior.

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

SENTINEL_CONTENT = "SYNTHETIC_PRIVATE_KEY_SENTINEL_DO_NOT_PACKAGE"
PRIVATE_KEY_PATTERNS = ["*.pem", "*.key", "*.p12", "*.pfx"]


def create_synthetic_fixtures(extension_dir: Path) -> list[Path]:
    """Create synthetic private key files with sentinel content."""
    fixtures = []
    for pattern in PRIVATE_KEY_PATTERNS:
        ext = pattern[1:]  # Remove *
        fixture = extension_dir / f"test_private{ext}"
        fixture.write_text(SENTINEL_CONTENT, encoding="utf-8")
        fixtures.append(fixture)
    return fixtures


def build_distributions(work_dir: Path) -> tuple[Path, Path]:
    """Build wheel and sdist in isolated directory."""
    dist_dir = work_dir / "dist"
    dist_dir.mkdir()

    # Build using project's build-system configuration
    result = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(dist_dir)],
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
    extension_dir = project_root / "src" / "browsertap_mcp" / "chrome_extension"

    if not extension_dir.exists():
        print(f"Extension directory not found: {extension_dir}", file=sys.stderr)
        sys.exit(1)

    with tempfile.TemporaryDirectory(prefix="packaging_test_") as tmpdir:
        work_dir = Path(tmpdir)

        # Create synthetic private key fixtures
        print("Creating synthetic private key fixtures...")
        fixtures = create_synthetic_fixtures(extension_dir)
        print(f"  Created {len(fixtures)} sentinel files")

        try:
            # Build distributions
            print("\nBuilding distributions...")
            wheel_path, sdist_path = build_distributions(work_dir)
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

        finally:
            # Clean up synthetic fixtures
            for fixture in fixtures:
                fixture.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
