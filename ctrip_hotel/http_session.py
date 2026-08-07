"""curl_cffi Session pool — connection reuse for hybrid HTTP batches.

Learned from curl_cffi docs + goofish-scrape:
  always prefer Session over one-off requests for cookie + TCP reuse.
Sessions are NOT thread-safe across threads → one Session per thread.
"""

from __future__ import annotations

import threading
from typing import Any

from curl_cffi import requests as cffi_requests

_local = threading.local()
_DEFAULT_IMPERSONATE = "chrome"


def get_thread_session(
    *,
    impersonate: str = _DEFAULT_IMPERSONATE,
    proxies: dict[str, str] | None = None,
    tag: str = "default",
) -> Any:
    """Return a thread-local curl_cffi Session (created once per thread+tag)."""
    key = f"sess::{tag}::{impersonate}::{id(proxies) if proxies else 0}"
    bag = getattr(_local, "sessions", None)
    if bag is None:
        bag = {}
        _local.sessions = bag
    sess = bag.get(key)
    if sess is None:
        kwargs: dict[str, Any] = {"impersonate": impersonate}
        # proxies applied per-request so one session can stay generic
        sess = cffi_requests.Session(**kwargs)
        bag[key] = sess
    return sess


def session_post(
    url: str,
    *,
    data: Any = None,
    json: Any = None,
    headers: dict[str, str] | None = None,
    proxies: dict[str, str] | None = None,
    timeout: float = 20,
    impersonate: str = _DEFAULT_IMPERSONATE,
    tag: str = "default",
) -> Any:
    """POST via thread-local Session (connection reuse)."""
    sess = get_thread_session(impersonate=impersonate, proxies=proxies, tag=tag)
    kwargs: dict[str, Any] = {
        "headers": headers or {},
        "timeout": timeout,
        "impersonate": impersonate,
    }
    if proxies:
        kwargs["proxies"] = proxies
    if json is not None:
        kwargs["json"] = json
    if data is not None:
        kwargs["data"] = data
    return sess.post(url, **kwargs)
