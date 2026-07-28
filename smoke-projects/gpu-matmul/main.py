#!/usr/bin/env python3
"""Deterministic CUDA matrix-multiplication smoke test."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import platform
import time


def run(run_id: str, size: int) -> dict[str, object]:
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available")

    torch.manual_seed(20260727)
    torch.backends.cuda.matmul.allow_tf32 = False
    device = torch.device("cuda:0")
    left_cpu = torch.randn((size, size), dtype=torch.float32)
    right_cpu = torch.randn((size, size), dtype=torch.float32)
    left = left_cpu.to(device)
    right = right_cpu.to(device)

    for _ in range(2):
        torch.matmul(left, right)
    torch.cuda.synchronize()
    started = time.perf_counter()
    result = None
    for _ in range(5):
        result = torch.matmul(left, right)
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - started
    assert result is not None

    expected = torch.matmul(left_cpu, right_cpu)
    actual = result.cpu()
    torch.testing.assert_close(actual, expected, rtol=1e-3, atol=1e-3)
    digest = hashlib.sha256(actual.numpy().tobytes()).hexdigest()
    maximum_error = float((actual - expected).abs().max().item())

    return {
        "ok": True,
        "kind": "gpu-matmul",
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "torch": torch.__version__,
        "cuda_runtime": torch.version.cuda,
        "device": torch.cuda.get_device_name(0),
        "matrix_size": size,
        "iterations": 5,
        "elapsed_seconds": elapsed,
        "max_abs_error": maximum_error,
        "result_sha256": digest,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--size", type=int, default=1024)
    args = parser.parse_args()
    try:
        result = run(args.run_id, args.size)
    except Exception as exc:
        result = {
            "ok": False,
            "kind": "gpu-matmul",
            "run_id": args.run_id,
            "error_type": type(exc).__name__,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
