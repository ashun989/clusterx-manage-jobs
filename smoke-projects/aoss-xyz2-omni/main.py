#!/usr/bin/env python3
"""Read/write/delete smoke test for the xyz2 AOSS mount."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

TARGET_ROOT = Path("/oss/xyz2-omni")
KIND = "aoss-xyz2-omni"


def payload() -> bytes:
    block = hashlib.sha256(b"clusterx-storage-smoke-20260727").digest()
    return (block * ((1024 * 1024 // len(block)) + 1))[: 1024 * 1024]


def retry(action, attempts: int = 10):
    error = None
    for _ in range(attempts):
        try:
            return action()
        except (FileNotFoundError, OSError) as exc:
            error = exc
            time.sleep(2)
    raise RuntimeError(f"eventual-consistency retry exhausted: {error}")


def run(target_root: Path, run_id: str) -> dict[str, object]:
    data = payload()
    expected = hashlib.sha256(data).hexdigest()
    directory = target_root / ".clusterx-smoke" / run_id / KIND
    path = directory / "payload.bin"
    directory.mkdir(parents=True, exist_ok=False)
    cleanup_ok = False
    try:
        path.write_bytes(data)
        actual = retry(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
        if actual != expected:
            raise RuntimeError("read-back SHA-256 mismatch")
    finally:
        retry(lambda: path.unlink()) if path.exists() else None
        retry(lambda: directory.rmdir())
        retry(lambda: directory.parent.rmdir())
        smoke_root = target_root / ".clusterx-smoke"
        if smoke_root.exists() and not any(smoke_root.iterdir()):
            retry(lambda: smoke_root.rmdir())
        cleanup_ok = not path.exists() and not directory.exists()
    if not cleanup_ok:
        raise RuntimeError("test artifact cleanup failed")
    return {"ok": True, "kind": KIND, "run_id": run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bytes": len(data), "sha256": expected, "cleanup": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--target", type=Path, default=TARGET_ROOT)
    args = parser.parse_args()
    try:
        result = run(args.target, args.run_id)
    except Exception as exc:
        result = {"ok": False, "kind": KIND, "run_id": args.run_id,
                  "error_type": type(exc).__name__, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
