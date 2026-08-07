"""Network helpers: proxy reachability checks for fail-fast intl warmup."""

from __future__ import annotations

import socket
import sys
from typing import Any
from urllib.parse import urlparse


def parse_proxy_host_port(proxy: str | None) -> tuple[str, int] | None:
    if not proxy:
        return None
    s = str(proxy).strip()
    if not s:
        return None
    if "://" not in s:
        s = f"http://{s}"
    u = urlparse(s)
    host = u.hostname
    if not host:
        return None
    port = u.port
    if port is None:
        port = 443 if u.scheme == "https" else 80
    return host, int(port)


def proxy_reachable(proxy: str | None, *, timeout: float = 1.5) -> tuple[bool, str]:
    """TCP connect check. Returns (ok, message). Empty proxy → (True, 'direct')."""
    parsed = parse_proxy_host_port(proxy)
    if not parsed:
        return True, "direct"
    host, port = parsed
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} ok"
    except OSError as e:
        return False, f"{host}:{port} unreachable ({e})"


def resolve_intl_proxy(cfg: dict[str, Any]) -> str | None:
    proxy = cfg.get("intl_proxy") or cfg.get("proxy")
    if not proxy:
        return None
    s = str(proxy).strip()
    return s or None


def intl_proxy_ready(cfg: dict[str, Any], *, timeout: float = 1.5) -> tuple[bool, str]:
    """Whether intl_price can proceed: proxy configured and TCP-reachable."""
    proxy = resolve_intl_proxy(cfg)
    if not proxy:
        return False, "未配置 intl_proxy / proxy（国际站需要境外出口）"
    ok, msg = proxy_reachable(proxy, timeout=timeout)
    if not ok:
        return False, f"代理不可达：{msg}"
    return True, msg


def popup_yes_no(title: str, message: str) -> bool | None:
    """Windows Yes/No dialog. True=Yes, False=No, None=unavailable/failed."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        # MB_YESNO | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND
        flags = 0x04 | 0x30 | 0x40000 | 0x10000
        result = ctypes.windll.user32.MessageBoxW(None, message, title, flags)
        # IDYES=6, IDNO=7
        if result == 6:
            return True
        if result == 7:
            return False
        return False
    except Exception:
        return None


def confirm_intl_without_proxy(detail: str) -> str:
    """Ask user what to do when intl proxy is missing/dead.

    Returns: 'domestic' | 'abort'
    """
    title = "携程爬虫 · 国际版代理未就绪"
    message = (
        f"{detail}\n\n"
        "国际版逐房型价格需要可用的境外代理，当前无法开启。\n\n"
        "是 = 仅抓国内数据（关闭国际价）继续\n"
        "否 = 取消本次抓取\n\n"
        "请先启动 Clash / V2Ray 等，或检查 config.yaml 的 intl_proxy。"
    )
    choice = popup_yes_no(title, message)
    if choice is True:
        return "domestic"
    if choice is False:
        return "abort"
    # 无 GUI 时：默认只跑国内，避免卡死
    print(f"[intl] {detail} → 自动降级为仅国内（无弹窗环境）", flush=True)
    return "domestic"
