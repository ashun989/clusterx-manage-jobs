#!/usr/bin/env python3
"""Build independently publishable Server, Web, Client and Skill artifacts."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def archive_directory(source: Path, destination: Path, root_name: str) -> str:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as bundle:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            info = bundle.gettarinfo(
                str(path), arcname=str(Path(root_name) / path.relative_to(source)),
            )
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            with path.open("rb") as source_file:
                bundle.addfile(info, source_file)
    encoded = gzip.compress(buffer.getvalue(), mtime=0)
    destination.write_bytes(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    destination.with_suffix(destination.suffix + ".sha256").write_text(
        f"{digest}  {destination.name}\n", encoding="utf-8",
    )
    return digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "release")
    parser.add_argument(
        "--no-isolation",
        action="store_true",
        help="reuse installed build dependencies (useful for offline builds)",
    )
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    run(["python3", "scripts/check_versions.py"])

    with tempfile.TemporaryDirectory(prefix="clusterx-release-") as directory:
        staging = Path(directory)
        web_dist = staging / "web"
        environment = {**os.environ, "CLUSTERX_WEB_OUT_DIR": str(web_dist)}
        run(["npm", "run", "build"], cwd=ROOT / "web", env=environment)
        build_options = ["--no-isolation"] if args.no_isolation else []
        run(["python3", "-m", "build", *build_options, "--outdir", str(output)], cwd=ROOT / "server")
        run(["python3", "-m", "build", *build_options, "--outdir", str(output)], cwd=ROOT / "client")
        run(["python3", "scripts/package_skill.py", "--output-dir", str(output)])
        web_version = (ROOT / "web/VERSION").read_text(encoding="utf-8").strip()
        archive_directory(
            web_dist, output / f"clusterx-monitor-web-{web_version}.tar.gz",
            "clusterx-monitor-web",
        )

    manifest = {
        "schema_version": 1,
        "server_version": (ROOT / "server/VERSION").read_text(encoding="utf-8").strip(),
        "web_version": (ROOT / "web/VERSION").read_text(encoding="utf-8").strip(),
        "client_version": (ROOT / "client/VERSION").read_text(encoding="utf-8").strip(),
        "skill_version": (ROOT / "skills/clusterx-manage-jobs/VERSION").read_text(encoding="utf-8").strip(),
    }
    (output / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps({"output": str(output), "manifest": manifest}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
