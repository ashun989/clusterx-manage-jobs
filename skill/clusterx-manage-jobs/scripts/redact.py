#!/usr/bin/env python3
"""Redact Clusterx, storage, and signed-URL credentials from stdin."""

from __future__ import annotations

import re
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SECRET_KEYS = (
    "ak_id",
    "ak_secret",
    "storage_ak_id",
    "storage_ak_secret",
    "secret_key",
    "access_key",
    "access_key_id",
    "app_secret",
    "client_secret",
    "password",
    "passwd",
    "private_key",
    "authorization",
    "cookie",
    "set_cookie",
    "session",
    "session_id",
    "token",
    "access_token",
    "refresh_token",
    "tenant_access_token",
    "user_access_token",
    "signature",
    "credential",
    "x_amz_credential",
    "x_amz_signature",
    "x_amz_security_token",
    "x_oss_signature",
    "awsaccesskeyid",
)

NORMALIZED_SECRET_KEYS = {
    re.sub(r"[^a-z0-9]", "", key.lower()) for key in SECRET_KEYS
}


def is_secret_key(key: str) -> bool:
    return re.sub(r"[^a-z0-9]", "", key.lower()) in NORMALIZED_SECRET_KEYS


def redact_urls(text: str) -> str:
    url_re = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ".,);]":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        try:
            parts = urlsplit(raw)
            query = parse_qsl(parts.query, keep_blank_values=True)
        except ValueError:
            return match.group(0)
        changed = False
        safe_query = []
        for key, value in query:
            if is_secret_key(key):
                value = "<redacted>"
                changed = True
            safe_query.append((key, value))
        netloc = parts.netloc
        if parts.username is not None or parts.password is not None:
            host = parts.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            if parts.port is not None:
                host = f"{host}:{parts.port}"
            netloc = f"<redacted>@{host}"
            changed = True
        if not changed:
            return match.group(0)
        return urlunsplit(
            (parts.scheme, netloc, parts.path, urlencode(safe_query), parts.fragment)
        ) + trailing

    return url_re.sub(replace, text)


def redact(text: str) -> str:
    text = redact_urls(text)
    keys = "|".join(re.escape(key) for key in SECRET_KEYS)

    # PEM private keys and common HTTP authentication headers.
    text = re.sub(
        r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
        r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
        "<redacted-private-key>",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"(?im)^(\s*(?:authorization|proxy-authorization)\s*:\s*)\S+[^\r\n]*",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        r"(?im)^(\s*(?:cookie|set-cookie)\s*:\s*)[^\r\n]*",
        r"\1<redacted>",
        text,
    )

    # Clusterx mount metadata uses {"key":"secret_key","value":"..."}.
    text = re.sub(
        rf'(?is)(["\']key["\']\s*:\s*["\'](?:{keys})["\']\s*,\s*'
        r'["\']value["\']\s*:\s*)(["\'])(.*?)(\2)',
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>{match.group(2)}",
        text,
    )

    # JSON, YAML, shell assignments, and CLI key/value forms.
    patterns = (
        re.compile(
            rf'(?i)(["\']?(?:{keys})["\']?\s*[:=]\s*)(["\']?)([^,\s}}\]\n;&"\']+|[^"\']*)(\2)'
        ),
        re.compile(
            rf"(?i)(--(?:{keys.replace('_', '[-_]').replace('|', '|')})(?:\s+|=))(\S+)"
        ),
    )

    def value_repl(match: re.Match[str]) -> str:
        if match.re is patterns[0]:
            quote = match.group(2)
            return f"{match.group(1)}{quote}<redacted>{quote}"
        return f"{match.group(1)}<redacted>"

    for pattern in patterns:
        text = pattern.sub(value_repl, text)
    return text


def main() -> int:
    sys.stdout.write(redact(sys.stdin.read()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
