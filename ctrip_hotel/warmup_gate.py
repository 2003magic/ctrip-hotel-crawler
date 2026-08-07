"""Warmup probe diagnosis + remediation hints."""

from __future__ import annotations

from typing import Any

DOMESTIC_PLAYBOOK: dict[str, tuple[str, str, str]] = {
    "empty_token": (
        "phantom-token 为空",
        "recapture",
        "设置 api_headed=true / --headed 手动过人机验证，或换 seed_hotel_id",
    ),
    "risk_203": (
        "国内风控码 203（人机/IP 墙）",
        "recapture",
        "api_headed=true 过滑块；或换网络/代理后重试；稍等几分钟再跑",
    ),
    "risk_4030": (
        "国内风控码 4030（token 无效/过期）",
        "recapture",
        "重新预热拿新 token；若反复出现则 headed 过验证或换 seed",
    ),
    "risk_other": (
        "国内接口返回其它风控码",
        "recapture",
        "检查日期/城市是否合法；headed 过验证；换 seed_hotel_ids",
    ),
    "no_rooms": (
        "探针酒店无房态数据",
        "next_seed",
        "换一个在售酒店作 seed_hotel_id（当前 seed 可能下架/无房）",
    ),
    "network": (
        "网络/超时/非 JSON",
        "backoff",
        "检查本机网络；国内列表可直连，勿误走坏代理",
    ),
    "template_missing": (
        "未捕获到房态请求模板",
        "recapture",
        "headed 过验证；扩充 seed_hotel_ids；稍后重试",
    ),
    "empty": (
        "空响应",
        "backoff",
        "稍后重试；或 headed 预热",
    ),
}

INTL_PLAYBOOK: dict[str, tuple[str, str, str]] = {
    "proxy_dead": (
        "境外代理连不上（ERR_PROXY_*）",
        "abort",
        "启动代理并把 intl_proxy 指到可用地址（如 http://127.0.0.1:7897）",
    ),
    "session": (
        "WhaleGuard/430 会话拦截",
        "rewarm",
        "保持 intl_headed=true；确认代理是境外出口；换 seed 后重试",
    ),
    "token": (
        "国际价 token 4030（签名失效/复用）",
        "resign_or_rewarm",
        "已自动重签；仍失败则 rewarm；检查系统时间是否准确",
    ),
    "no_rooms": (
        "探针酒店国际站无售卖房型",
        "next_seed",
        "换 seed 为列表里确认有价的酒店 ID",
    ),
    "signature": (
        "页面未暴露 window.signature",
        "rewarm",
        "intl_headed=true；代理畅通；换 seed_hotel_id",
    ),
    "network": (
        "网络/超时/非 JSON",
        "backoff",
        "检查 intl_proxy 与境外连通性",
    ),
    "empty": (
        "空响应",
        "rewarm",
        "rewarm + 换 seed；确认代理出口未被拉黑",
    ),
    "template_missing": (
        "未捕获 Oversea 请求模板",
        "rewarm",
        "代理/headed/换 seed；详见 intl warmup 日志",
    ),
}



def classify_domestic_probe(
    payload: dict[str, Any] | None,
    *,
    token: str = "",
) -> tuple[str, str]:
    if not str(token or "").strip():
        return "empty_token", "phantom-token empty"
    if not payload:
        return "empty", "empty payload"
    err = str(payload.get("error") or "")
    if err:
        return "network", err[:160]
    data = payload.get("data")
    if not isinstance(data, dict):
        return "empty", "no data object"
    code = data.get("htlSpiderActionErrorCode")
    if code not in (None, "", 0, "0"):
        c = str(code)
        if c == "203":
            return "risk_203", f"risk_code={c}"
        if c == "4030":
            return "risk_4030", f"risk_code={c}"
        return "risk_other", f"risk_code={c}"
    n_sale = len(data.get("saleRoomMap") or {})
    n_phys = len(data.get("physicRoomMap") or {})
    if n_sale or n_phys:
        return "ok", f"sale={n_sale} physic={n_phys}"
    return "no_rooms", "no saleRoomMap/physicRoomMap"


def classify_intl_probe(
    payload: dict[str, Any] | None, *, kind: str = ""
) -> tuple[str, str]:
    if not payload:
        return "empty", "empty payload"
    err = str(payload.get("error") or "")
    low = err.lower()
    if any(
        x in low
        for x in (
            "err_proxy",
            "proxy connection",
            "tunnel_connection",
            "proxy not ready",
        )
    ):
        return "proxy_dead", err[:160]
    if "signature" in low and (
        "missing" in low or "invalid" in low or "failed" in low
    ):
        return "signature", err[:160]
    if kind == "session" or "430" in low or "whaleguard" in low:
        return "session", kind or err[:160]
    if kind == "token":
        return "token", "4030"
    if kind == "ok":
        n = len((payload.get("data") or {}).get("saleRoomMap") or {})
        return "ok", f"sale={n}"
    if kind == "empty":
        return "no_rooms", "no saleRoomMap"
    if err:
        return "network", err[:160]
    if kind == "error":
        return "network", err[:160] or "error"
    return "empty", kind or "unknown"


def print_probe_diagnosis(
    *,
    channel: str,
    reason: str,
    detail: str,
    action: str,
    hint: str,
    attempt: int,
    attempts: int,
    hotel_id: Any = None,
) -> None:
    playbook = DOMESTIC_PLAYBOOK if channel == "api" else INTL_PLAYBOOK
    title = playbook[reason][0] if reason in playbook else reason
    hid = f" hotel={hotel_id}" if hotel_id is not None else ""
    print(
        f"  [{channel}] probe 失败 ({attempt}/{attempts}){hid}\n"
        f"         原因: {title} [{reason}] {detail}\n"
        f"         自动修复: {action}\n"
        f"         若仍失败: {hint}",
        flush=True,
    )


def final_probe_error(
    *,
    channel: str,
    reason: str,
    detail: str,
    hotel_id: Any,
    tried: list[str],
) -> RuntimeError:
    playbook = DOMESTIC_PLAYBOOK if channel == "api" else INTL_PLAYBOOK
    title, _, hint = playbook.get(reason, (reason, "", "查看上方 probe 日志"))
    tried_s = " -> ".join(tried) if tried else "(无)"
    label = "API" if channel == "api" else "国际价"
    msg = (
        f"{label}预热探针未通过，已阻止批量抓取。\n"
        f"  最终原因: {title} [{reason}] {detail}\n"
        f"  探针酒店: {hotel_id}\n"
        f"  已尝试: {tried_s}\n"
        f"  建议: {hint}"
    )
    return RuntimeError(msg)
