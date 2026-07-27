#!/usr/bin/env python3
"""Emit flushed progress logs and persist a final sanitized result."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import time


def emit(event: dict[str, object]) -> None:
    print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)


def run(
    run_id: str,
    *,
    steps: int,
    interval_seconds: float,
    result_file: Path,
) -> dict[str, object]:
    started = time.monotonic()
    emit(
        {
            "event": "started",
            "run_id": run_id,
            "steps": steps,
            "interval_seconds": interval_seconds,
            "python": platform.python_version(),
        }
    )
    for step in range(1, steps + 1):
        time.sleep(interval_seconds)
        emit(
            {
                "event": "progress",
                "run_id": run_id,
                "step": step,
                "steps": steps,
                "elapsed_seconds": time.monotonic() - started,
            }
        )

    result = {
        "ok": True,
        "kind": "ssp-live-log",
        "run_id": run_id,
        "steps": steps,
        "elapsed_seconds": time.monotonic() - started,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    result_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = result_file.with_suffix(result_file.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(result_file)
    emit({"event": "completed", **result, "result_file": str(result_file)})
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--steps", type=int, default=12)
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--result-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(
            args.run_id,
            steps=args.steps,
            interval_seconds=args.interval_seconds,
            result_file=args.result_file,
        )
    except Exception as exc:
        emit(
            {
                "ok": False,
                "event": "failed",
                "kind": "ssp-live-log",
                "run_id": args.run_id,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
