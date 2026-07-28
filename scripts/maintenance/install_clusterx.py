#!/usr/bin/env python3
"""Developer helper to fetch and install the Clusterx wheel documented in Feishu."""

from __future__ import annotations

import argparse
from email.parser import Parser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit
from urllib.request import build_opener, ProxyHandler, Request
import zipfile


DEFAULT_MANIFEST = Path(__file__).with_name("sources.json")
WHEEL_URL = re.compile(r"https?://[^\s\"'<>]+?\.whl(?:\?[^\s\"'<>]*)?")
ALLOWED_HTTP_WHEEL_ENDPOINTS = {
    ("xceph-outside.pjlab.org.cn", 8060),
}
MAX_WHEEL_BYTES = 1024 * 1024 * 1024
PROXY_VARIABLES = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


def find_source(manifest: dict, source_id: str) -> dict:
    matches = [
        source
        for source in manifest.get("sources", [])
        if source.get("id") == source_id
    ]
    if len(matches) != 1 or not matches[0].get("url"):
        raise ValueError(f"unknown or incomplete source: {source_id}")
    return matches[0]


def direct_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in PROXY_VARIABLES
    }


def fetch_document(url: str, *, keep_proxy: bool = False) -> dict:
    lark_cli = shutil.which("lark-cli")
    if not lark_cli:
        raise RuntimeError("lark-cli is not installed")
    environment = None if keep_proxy else direct_environment()
    auth = subprocess.run(
        [lark_cli, "auth", "status"],
        text=True,
        capture_output=True,
        timeout=30,
        env=environment,
    )
    if auth.returncode != 0:
        raise RuntimeError("Feishu authentication is unavailable")
    fetch = subprocess.run(
        [
            lark_cli,
            "docs",
            "+fetch",
            "--doc",
            url,
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
        env=environment,
    )
    if fetch.returncode != 0:
        raise RuntimeError("Feishu document fetch failed")
    return json.loads(fetch.stdout)


def document_content(payload: dict) -> str:
    candidates = (
        payload.get("data", {}).get("document", {}),
        payload.get("document", {}),
        payload.get("data", {}),
    )
    for document in candidates:
        if isinstance(document, dict) and isinstance(document.get("content"), str):
            return document["content"]
    raise ValueError("fetch response has no document content")


def extract_wheel_url(payload: dict) -> str:
    matches = WHEEL_URL.findall(document_content(payload))
    if len(matches) != 1:
        raise ValueError("expected exactly one HTTP(S) wheel URL")
    url = matches[0].replace("&amp;", "&")
    wheel_filename(url)
    return url


def wheel_filename(url: str) -> str:
    parsed = urlsplit(url)
    name = Path(unquote(parsed.path)).name
    if parsed.username or parsed.password:
        raise ValueError("wheel URL must not contain user information")
    if parsed.scheme == "http":
        endpoint = (parsed.hostname or "", parsed.port or 80)
        if endpoint not in ALLOWED_HTTP_WHEEL_ENDPOINTS:
            raise ValueError("HTTP wheel URL endpoint is not trusted")
    elif parsed.scheme != "https":
        raise ValueError("wheel URL must use HTTPS or an approved HTTP endpoint")
    if not name.endswith(".whl"):
        raise ValueError("wheel URL must end in .whl")
    return name


def validate_clusterx_wheel(path: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(path) as wheel:
            metadata_files = [
                name
                for name in wheel.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_files) != 1:
                raise ValueError("wheel must contain exactly one METADATA file")
            metadata = Parser().parsestr(
                wheel.read(metadata_files[0]).decode("utf-8")
            )
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise ValueError("downloaded file is not a valid wheel") from exc
    if metadata.get("Name", "").lower() != "clusterx":
        raise ValueError("wheel package name is not clusterx")
    return {
        "name": metadata["Name"],
        "version": metadata.get("Version", "unknown"),
        "requires_python": metadata.get("Requires-Python", "unknown"),
    }


def download(url: str, destination: Path, *, keep_proxy: bool = False) -> None:
    request = Request(url, headers={"User-Agent": "clusterx-skill-maintainer"})
    opener = build_opener() if keep_proxy else build_opener(ProxyHandler({}))
    total = 0
    with opener.open(request, timeout=120) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_WHEEL_BYTES:
                raise ValueError("wheel exceeds download size limit")
            output.write(chunk)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable from the target isolated environment",
    )
    parser.add_argument("--source", default="clusterx-main")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument(
        "--keep-proxy",
        action="store_true",
        help="inherit proxy variables instead of connecting directly",
    )
    args = parser.parse_args()
    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        source = find_source(manifest, args.source)
        payload = fetch_document(source["url"], keep_proxy=args.keep_proxy)
        url = extract_wheel_url(payload)
        name = wheel_filename(url)
        with tempfile.TemporaryDirectory(prefix="clusterx-wheel-") as directory:
            wheel = Path(directory) / name
            download(url, wheel, keep_proxy=args.keep_proxy)
            metadata = validate_clusterx_wheel(wheel)
            install = subprocess.run(
                [args.python, "-m", "pip", "install", str(wheel)]
            )
            if install.returncode != 0:
                raise RuntimeError("pip installation failed")
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error_type": type(exc).__name__},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"ok": True, **metadata}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
