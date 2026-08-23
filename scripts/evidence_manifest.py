"""Bind release evidence artifacts to one exact Git/worktree source state."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "artifacts" / "evidence-manifest.json"
# 2: the source record gained `missing_file_count`, and `content_sha256` now
# covers tracked-but-deleted paths. A version-1 manifest is not comparable
# against a version-2 fingerprint, so it has to be rejected by version rather
# than silently reported as a content mismatch.
SCHEMA_VERSION = 2
OFFLINE_ARTIFACTS = (
    "artifacts/coverage.json",
    "artifacts/offline-junit.xml",
    "artifacts/tool-coverage-offline.json",
)
LIVE_ARTIFACTS = (
    "artifacts/live-junit.xml",
    "artifacts/tool-coverage-live.json",
    # The junit says which tests passed; this says whether the run was worth
    # believing -- which build each of the three processes was running, whether
    # the browser was idle, and whether the tab inventory came back the way it
    # went in. It was written beside the others and uploaded by `live.yml`, but
    # nothing bound it, so a seal could pair a passing suite with a preflight
    # record left over from an older run, or with none at all. A live run that
    # never reached the session fixture writes no such file, and sealing then
    # fails naming it rather than sealing the half that happens to exist.
    "artifacts/live-preflight.json",
)


def _git(*args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def _source_paths() -> list[tuple[str, Path | None]]:
    """List every tracked or untracked-but-not-ignored source path.

    Entries whose file is absent keep a ``None`` path instead of being dropped.
    Dropping them made two different trees fingerprint identically: one where a
    tracked release file had been deleted, and one where that file never
    existed. `file_count` also reported only the surviving files, so the record
    carried no sign that Git tracks paths the fingerprint ignored.
    """
    output = subprocess.run(
        ("git", "ls-files", "-co", "--exclude-standard", "-z"),
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if output.returncode != 0:
        raise RuntimeError("git ls-files failed while building the source fingerprint")
    entries: dict[str, Path | None] = {}
    for raw in output.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="surrogateescape")
        path = ROOT / relative
        entries[Path(relative).as_posix()] = path if path.is_file() else None
    return sorted(entries.items(), key=lambda item: item[0])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_identity() -> dict[str, Any]:
    entries = _source_paths()
    digest = hashlib.sha256()
    present = 0
    missing = 0
    for relative, path in entries:
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        if path is None:
            digest.update(b"<absent>")
            missing += 1
        else:
            digest.update(_sha256(path).encode("ascii"))
            present += 1
        digest.update(b"\n")
    status = _git("status", "--porcelain=v1", "--untracked-files=all")
    return {
        "git_head": _git("rev-parse", "HEAD").strip(),
        "git_dirty": bool(status.strip()),
        "content_sha256": digest.hexdigest(),
        "file_count": present,
        "missing_file_count": missing,
    }


def _artifact_paths(*, include_live: bool) -> list[Path]:
    relative_paths = list(OFFLINE_ARTIFACTS)
    if include_live:
        relative_paths.extend(LIVE_ARTIFACTS)
    paths = [ROOT / relative for relative in relative_paths]
    dist = ROOT / "artifacts" / "dist"
    if dist.is_dir():
        paths.extend(sorted(path for path in dist.iterdir() if path.is_file()))
    return paths


def build_manifest(*, include_live: bool) -> dict[str, Any]:
    artifacts: dict[str, dict[str, Any]] = {}
    missing = []
    for path in _artifact_paths(include_live=include_live):
        relative = path.relative_to(ROOT).as_posix()
        if not path.is_file():
            missing.append(relative)
            continue
        artifacts[relative] = {"sha256": _sha256(path), "bytes": path.stat().st_size}
    if missing:
        raise FileNotFoundError(f"release evidence artifacts missing: {', '.join(missing)}")
    if not any(name.startswith("artifacts/dist/") for name in artifacts):
        raise FileNotFoundError("release evidence artifacts missing: artifacts/dist/*")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated": datetime.now(timezone.utc).isoformat(),
        "include_live": include_live,
        "source": source_identity(),
        "artifacts": artifacts,
    }


def write_manifest(path: Path = DEFAULT_OUTPUT, *, include_live: bool) -> dict[str, Any]:
    manifest = build_manifest(include_live=include_live)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix="evidence-", suffix=".json", delete=False
    ) as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)
    return manifest


def validate_manifest(
    path: Path = DEFAULT_OUTPUT,
    *,
    require_live: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"evidence manifest unavailable: {type(exc).__name__}"]
    problems = []
    recorded_schema = manifest.get("schema_version")
    if recorded_schema != SCHEMA_VERSION:
        problems.append(
            f"unsupported evidence manifest schema: {recorded_schema!r} "
            f"(expected {SCHEMA_VERSION}); re-seal with the current tooling"
        )
    if require_live and manifest.get("include_live") is not True:
        problems.append("evidence manifest does not include live results")
    try:
        current_source = source_identity()
    except RuntimeError as exc:
        problems.append(str(exc))
    else:
        if manifest.get("source") != current_source:
            problems.append("source tree no longer matches the evidence manifest")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        problems.append("evidence manifest artifacts must be an object")
        return manifest, problems
    required = set(OFFLINE_ARTIFACTS)
    if manifest.get("include_live") is True:
        required.update(LIVE_ARTIFACTS)
    recorded = set(artifacts)
    dist_records = {name for name in recorded if name.startswith("artifacts/dist/")}
    expected = required | dist_records
    missing_records = sorted(required - recorded)
    if missing_records:
        problems.append(f"artifact records missing: {', '.join(missing_records)}")
    extra_records = sorted(recorded - expected)
    if extra_records:
        problems.append(f"unexpected artifact records: {', '.join(extra_records)}")
    wheels = sorted(name for name in dist_records if name.endswith(".whl"))
    sdists = sorted(name for name in dist_records if name.endswith((".tar.gz", ".tgz")))
    if len(wheels) != 1 or len(sdists) != 1 or len(dist_records) != 2:
        problems.append(
            "distribution records must contain exactly one wheel and one source archive"
        )
    for relative, record in artifacts.items():
        path_value = ROOT / relative
        if not path_value.is_file():
            problems.append(f"artifact missing: {relative}")
            continue
        expected = record.get("sha256") if isinstance(record, dict) else None
        if expected != _sha256(path_value):
            problems.append(f"artifact hash mismatch: {relative}")
        expected_bytes = record.get("bytes") if isinstance(record, dict) else None
        if expected_bytes != path_value.stat().st_size:
            problems.append(f"artifact size mismatch: {relative}")
    return manifest, problems


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-live", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    if args.check:
        manifest, problems = validate_manifest(output, require_live=args.include_live)
        print(json.dumps({"ok": not problems, "problems": problems, "manifest": manifest}, indent=2))
        return 0 if not problems else 1
    manifest = write_manifest(output, include_live=args.include_live)
    print(json.dumps({"output": str(output), "source": manifest["source"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
