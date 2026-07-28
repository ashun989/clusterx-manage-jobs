#!/usr/bin/env python3
"""Read, verify, and clean up file and object storage in one smoke task."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Callable, TypeVar


PAYLOAD_BYTES = 1024 * 1024
SAFE_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
T = TypeVar("T")


def payload() -> bytes:
    block = hashlib.sha256(b"clusterx-storage-smoke").digest()
    return (block * ((PAYLOAD_BYTES // len(block)) + 1))[:PAYLOAD_BYTES]


def retry(action: Callable[[], T], attempts: int = 10) -> T:
    error: OSError | None = None
    for attempt in range(attempts):
        try:
            return action()
        except OSError as exc:
            error = exc
            if attempt + 1 < attempts:
                time.sleep(2)
    raise RuntimeError("eventual-consistency retry exhausted") from error


def parse_target(value: str) -> tuple[str, str, Path]:
    try:
        storage_type, alias, raw_path = value.split(":", 2)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "target must use TYPE:ALIAS:PATH"
        ) from exc
    if storage_type not in {"file", "object"}:
        raise argparse.ArgumentTypeError("target type must be file or object")
    if not SAFE_LABEL.fullmatch(alias):
        raise argparse.ArgumentTypeError(
            "target alias must contain lowercase letters, digits, and hyphens"
        )
    path = Path(raw_path)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("target path must be absolute")
    return storage_type, alias, path


def remove_empty_parents(target_root: Path, directory: Path) -> None:
    run_root = directory.parent
    smoke_root = run_root.parent
    if directory.exists():
        directory.rmdir()
    if run_root.exists() and not any(run_root.iterdir()):
        run_root.rmdir()
    if smoke_root.exists() and not any(smoke_root.iterdir()):
        smoke_root.rmdir()
    if smoke_root.parent != target_root:
        raise RuntimeError("invalid cleanup boundary")


def check_target(
    storage_type: str,
    alias: str,
    target_root: Path,
    run_id: str,
) -> dict[str, object]:
    data = payload()
    expected = hashlib.sha256(data).hexdigest()
    directory = target_root / ".clusterx-smoke" / run_id / alias
    path = directory / "payload.bin"
    directory.mkdir(parents=True, exist_ok=False)
    try:
        with path.open("wb") as stream:
            stream.write(data)
            stream.flush()
            if storage_type == "file":
                os.fsync(stream.fileno())
        read_bytes = path.read_bytes
        actual = (
            retry(lambda: hashlib.sha256(read_bytes()).hexdigest())
            if storage_type == "object"
            else hashlib.sha256(read_bytes()).hexdigest()
        )
        if actual != expected:
            raise RuntimeError("read-back SHA-256 mismatch")
    finally:
        if storage_type == "object":
            if path.exists():
                retry(path.unlink)
            retry(lambda: remove_empty_parents(target_root, directory))
        else:
            if path.exists():
                path.unlink()
            remove_empty_parents(target_root, directory)

    return {
        "ok": True,
        "storage_type": storage_type,
        "alias": alias,
        "bytes": len(data),
        "sha256": expected,
        "cleanup": True,
    }


def run(
    targets: list[tuple[str, str, Path]],
    run_id: str,
) -> dict[str, object]:
    if not SAFE_LABEL.fullmatch(run_id):
        raise ValueError(
            "run ID must contain lowercase letters, digits, and hyphens"
        )
    target_types = {storage_type for storage_type, _, _ in targets}
    if target_types != {"file", "object"}:
        raise ValueError("at least one file and one object target are required")

    results: list[dict[str, object]] = []
    for storage_type, alias, target_root in targets:
        try:
            result = check_target(storage_type, alias, target_root, run_id)
        except Exception as exc:
            result = {
                "ok": False,
                "storage_type": storage_type,
                "alias": alias,
                "error_type": type(exc).__name__,
            }
        results.append(result)

    return {
        "ok": all(bool(result["ok"]) for result in results),
        "kind": "storage-access",
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "targets": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--target",
        action="append",
        required=True,
        type=parse_target,
        help="Runtime-only storage target in TYPE:ALIAS:PATH form",
    )
    args = parser.parse_args()
    try:
        result = run(args.target, args.run_id)
    except Exception as exc:
        result = {
            "ok": False,
            "kind": "storage-access",
            "run_id": args.run_id,
            "error_type": type(exc).__name__,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
