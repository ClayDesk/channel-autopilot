"""Tiny HTTP helper (stdlib only). Tests replace `request`."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .errors import AutopilotError


def request(method: str, url: str, data: dict | None = None, headers: dict | None = None, retries: int = 2, timeout: int = 120):
    body = None
    if data is not None and method != "GET":
        body = urllib.parse.urlencode(data).encode()
    elif data:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(data)
    return _send(method, url, body, headers or {}, retries, timeout)[0]


def call(method: str, url: str, body: dict | bytes | None = None, headers: dict | None = None, retries: int = 2, timeout: int = 120):
    """Like `request`, for JSON APIs: a dict body is sent as JSON, bytes as they are.
    Returns (parsed body, response headers with lower-case names)."""
    headers = dict(headers or {})
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    return _send(method, url, body, headers, retries, timeout)


def _send(method, url, body, headers, retries, timeout):
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode()
                return (json.loads(raw) if raw else {}), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            text = e.read().decode()[:600]
            if e.code >= 500 and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise AutopilotError(f"{method} {url.split('?')[0]}: HTTP {e.code} {text}") from None
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise AutopilotError(f"{method} {url.split('?')[0]}: {e.reason}") from None
