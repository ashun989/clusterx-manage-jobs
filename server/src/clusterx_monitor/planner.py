from __future__ import annotations

import time
from typing import Any

from .models import PlanRequest
from .planning import prepare_plan, solve_prepared_plan


def solve_plan(snapshot: dict[str, Any], request_payload: dict[str, Any]) -> dict[str, Any]:
    """Build and solve one immutable, snapshot-pinned CP-SAT planning problem."""
    if len(snapshot.get("nodes") or []) > 100_000 or len(snapshot.get("workloads") or []) > 100_000:
        raise ValueError("planner candidate snapshot exceeds the 100000-record safety limit")
    started = time.monotonic()
    request = PlanRequest.model_validate(request_payload)
    prepared = prepare_plan(snapshot, request)
    return solve_prepared_plan(snapshot, request, prepared, started)
