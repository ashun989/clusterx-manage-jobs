#!/usr/bin/env python3
"""Fetch a Feishu source into staging and report a sanitized candidate diff."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from redact import redact


SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = SKILL_DIR / "references" / "sources.json"
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def fail(message: str, code: int = 2) -> int:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False, indent=2))
    return code


def find_source(manifest: dict, source_id: str) -> dict | None:
    for source in manifest.get("sources", []):
        if source.get("id") == source_id:
            return source
    return None


def extract_document(payload: dict) -> tuple[str, object]:
    candidates = (
        payload.get("data", {}).get("document", {}),
        payload.get("document", {}),
        payload.get("data", {}),
    )
    for document in candidates:
        if isinstance(document, dict) and isinstance(document.get("content"), str):
            return document["content"], document.get("revision_id")
    raise ValueError("lark-cli response has no data.document.content")


def normalize(content: str) -> str:
    content = redact(content).replace("\r\n", "\n").replace("\r", "\n")
    content = "\n".join(line.rstrip() for line in content.splitlines()).strip()
    return content + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--staging-dir", required=True)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    args = parser.parse_args()

    if not SAFE_ID.fullmatch(args.source):
        return fail("source id must contain only lowercase letters, digits, and hyphens")

    manifest_path = Path(args.manifest).resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return fail(f"cannot read source manifest: {exc}")

    source = find_source(manifest, args.source)
    if source is None:
        return fail(f"unknown source: {args.source}")
    if not source.get("url"):
        return fail(
            f"source {args.source} has no URL; register the authoritative Feishu URL first"
        )

    lark_cli = shutil.which("lark-cli")
    if not lark_cli:
        return fail("lark-cli is not installed")

    auth = subprocess.run(
        [lark_cli, "auth", "status"],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if auth.returncode != 0:
        return fail("Feishu authentication is unavailable; run lark-cli auth login --recommend")

    fetch = subprocess.run(
        [
            lark_cli,
            "docs",
            "+fetch",
            "--doc",
            source["url"],
            "--doc-format",
            "markdown",
            "--detail",
            "simple",
            "--format",
            "json",
        ],
        text=True,
        capture_output=True,
        timeout=180,
    )
    if fetch.returncode != 0:
        return fail("Feishu document fetch failed: " + redact(fetch.stderr).strip())

    try:
        payload = json.loads(fetch.stdout)
        content, revision_id = extract_document(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        return fail(f"cannot parse lark-cli response: {exc}")

    candidate = normalize(content)
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    reference_path = (
        manifest_path.parent / str(source["reference_path"])
    ).resolve()
    try:
        current = reference_path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    current_normalized = current.replace("\r\n", "\n").replace("\r", "\n")
    changed = current_normalized != candidate

    staging = Path(args.staging_dir).resolve() / args.source
    staging.mkdir(parents=True, exist_ok=True)
    candidate_path = staging / "candidate.md"
    diff_path = staging / "diff.patch"
    report_path = staging / "report.json"
    candidate_path.write_text(candidate, encoding="utf-8")
    diff = "".join(
        difflib.unified_diff(
            current_normalized.splitlines(keepends=True),
            candidate.splitlines(keepends=True),
            fromfile=str(reference_path),
            tofile=str(candidate_path),
        )
    )
    diff_path.write_text(diff, encoding="utf-8")

    report = {
        "ok": True,
        "changed": changed,
        "source": args.source,
        "title": source.get("title"),
        "revision_id": revision_id,
        "sha256": digest,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "candidate": str(candidate_path),
        "diff": str(diff_path),
        "approved_reference": str(reference_path),
        "note": (
            "No skill files were modified; the sanitized candidate is staged "
            "for the caller to review and apply when the user requested an update."
        ),
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 10 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
