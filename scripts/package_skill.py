#!/usr/bin/env python3
"""Build a deterministic archive containing only the installable Skill."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill" / "clusterx-manage-jobs"
ARCHIVE_NAME = "clusterx-manage-jobs.tar.gz"
EXCLUDED_PARTS = {"__pycache__"}
FORBIDDEN_RUNTIME_TEXT = (
    "lark-cli",
    "auth login",
    "check_updates.py",
    "install_clusterx.py",
    "sources.json",
)


def included_files() -> list[Path]:
    files = [
        path
        for path in sorted(SKILL.rglob("*"))
        if path.is_file()
        and not EXCLUDED_PARTS.intersection(path.parts)
        and path.suffix not in {".pyc", ".pyo"}
    ]
    for path in files:
        try:
            text = path.read_text(encoding="utf-8").lower()
        except (OSError, UnicodeError):
            continue
        matches = [
            marker for marker in FORBIDDEN_RUNTIME_TEXT if marker in text
        ]
        if matches:
            raise RuntimeError(
                f"developer-only dependency in {path.relative_to(SKILL)}: "
                + ", ".join(matches)
            )
    return files


def build_archive(output_dir: Path) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / ARCHIVE_NAME
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w") as bundle:
        for path in included_files():
            relative = path.relative_to(SKILL)
            info = bundle.gettarinfo(
                str(path),
                arcname=str(Path(SKILL.name) / relative),
            )
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            with path.open("rb") as source:
                bundle.addfile(info, source)

    compressed = gzip.compress(tar_buffer.getvalue(), mtime=0)
    archive.write_bytes(compressed)
    digest = hashlib.sha256(compressed).hexdigest()
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return {
        "archive": str(archive),
        "checksum": str(checksum),
        "sha256": digest,
        "files": len(included_files()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    print(json.dumps(build_archive(args.output_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
