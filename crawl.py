# -*- coding: utf-8 -*-
"""Ctrip hotel crawler (API mode) - single file."""
from __future__ import annotations

import argparse
import csv
import json
import re
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import yaml
from curl_cffi import requests as cffi_requests

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)  # type: ignore[attr-defined]
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = ROOT / "config.example.yaml"

DEFAULTS: dict[str, Any] = {
    "city_id": 1,
    "city_name": "\u5317\u4eac",
    "check_in": None,
    "check_out": None,
    "pages": 1,
    "max_hotels": 20,
    "delay_ms": 1500,
    "headed": True,
    "verify_wait_sec": 180,
    "unlock_price": False,
    "open_room_detail": True,
    "room_detail_limit": 3,
    "browser_channel": "msedge",
    "output_dir": "data",
    "workers": 1,
    "workers_headed": False,
    "skip_done": True,
    "group_by": "round_robin",
    "mode": "api",
    "page_size": 20,
    "seed_hotel_id": 1,
    "api_headed": False,
    "api_workers": 8,
    "intl_price": True,
    "intl_workers": 4,
    "intl_headed": True,
    "intl_proxy": None,
    "intl_http_retries": 2,
    "intl_sign_batch": None,
    "intl_rewarm_after": 2,
    "proxy": None,
    "proxy_list": [],
    "proxy_api_url": None,
}


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg = deepcopy(DEFAULTS)
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not cfg_path.exists() and EXAMPLE_CONFIG_PATH.exists():
        cfg_path = EXAMPLE_CONFIG_PATH
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"config must be a mapping: {cfg_path}")
        cfg.update(raw)

    today = date.today()
    check_in = cfg.get("check_in") or (today + timedelta(days=7)).isoformat()
    check_out = cfg.get("check_out") or (
        date.fromisoformat(str(check_in)) + timedelta(days=1)
    ).isoformat()
    cfg["check_in"] = str(check_in)
    cfg["check_out"] = str(check_out)
    cfg["output_dir"] = str((ROOT / cfg["output_dir"]).resolve())
    return cfg


# ===== inlined from ctrip_hotel/store.py =====
def new_run_dir(output_dir: str | Path) -> Path:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    run = root / datetime.now().strftime("%Y%m%d_%H%M%S")
    run.mkdir(parents=True, exist_ok=False)
    return run


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    # stable union of keys
    keys: list[str] = []
    seen = set()
    for row in rows:
        for k in row:
            if k not in seen and k != "raw_summary":
                seen.add(k)
                keys.append(k)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)

# ===== inlined from ctrip_hotel/state.py =====
_LOCK = threading.Lock()


def state_dir(output_dir: str | Path) -> Path:
    d = Path(output_dir) / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def done_path(output_dir: str | Path) -> Path:
    return state_dir(output_dir) / "done.jsonl"


def done_key(
    *,
    city_id: int | str,
    hotel_id: int | str,
    check_in: str,
    check_out: str,
) -> str:
    return f"{city_id}:{hotel_id}:{check_in}:{check_out}"


def load_done(output_dir: str | Path) -> set[str]:
    path = done_path(output_dir)
    if not path.exists():
        return set()
    keys: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # allow plain key or {"key": "..."}
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    k = obj.get("key")
                    if k:
                        keys.add(str(k))
                except json.JSONDecodeError:
                    continue
            else:
                keys.add(line)
    return keys


def mark_done(
    output_dir: str | Path,
    key: str,
    *,
    meta: dict[str, Any] | None = None,
) -> None:
    path = done_path(output_dir)
    row = {"key": key, **(meta or {})}
    with _LOCK:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def filter_new_hotels(
    hotels: Iterable[dict[str, Any]],
    done: set[str],
    *,
    city_id: int | str,
    check_in: str,
    check_out: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (todo, skipped)."""
    todo: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for h in hotels:
        k = done_key(
            city_id=city_id,
            hotel_id=h["hotel_id"],
            check_in=check_in,
            check_out=check_out,
        )
        if k in done:
            skipped.append(h)
        else:
            todo.append(h)
    return todo, skipped


def split_groups(
    hotels: list[dict[str, Any]], workers: int
) -> list[list[dict[str, Any]]]:
    """Round-robin groups for workers; empty groups dropped by caller if needed."""
    n = max(int(workers), 1)
    groups: list[list[dict[str, Any]]] = [[] for _ in range(n)]
    for i, h in enumerate(hotels):
        groups[i % n].append(h)
    return groups


def dedupe_hotels(hotels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    uniq: dict[Any, dict[str, Any]] = {}
    for h in hotels:
        uniq[h["hotel_id"]] = h
    return list(uniq.values())

# ===== inlined from ctrip_hotel/http_session.py =====
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

# ===== inlined from ctrip_hotel/session_store.py =====
SESSION_DIR = ROOT / "data" / ".sessions"
# storage_state older than this is ignored (cookies go stale / risk flags).
DEFAULT_MAX_AGE_SEC = 6 * 3600


def _paths(channel: str) -> tuple[Path, Path]:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in channel)
    return SESSION_DIR / f"{safe}.storage.json", SESSION_DIR / f"{safe}.meta.json"


def load_storage_state(
    channel: str, *, max_age_sec: int = DEFAULT_MAX_AGE_SEC
) -> Path | None:
    """Return path to a fresh-enough storage_state file, else None."""
    state_path, meta_path = _paths(channel)
    if not state_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        saved_at = float(meta.get("saved_at") or state_path.stat().st_mtime)
        age = time.time() - saved_at
        if age > max_age_sec:
            print(f"  [session] {channel} storage_state expired ({age/3600:.1f}h)", flush=True)
            return None
        print(f"  [session] {channel} reuse storage_state age={age/60:.0f}m", flush=True)
        return state_path
    except Exception as e:
        print(f"  [session] {channel} load skip: {e}", flush=True)
        return None


def save_storage_state(channel: str, context: Any, *, extra: dict[str, Any] | None = None) -> None:
    """Serialize cookies/localStorage after a successful warmup+probe."""
    state_path, meta_path = _paths(channel)
    try:
        context.storage_state(path=str(state_path))
        meta = {
            "saved_at": time.time(),
            "channel": channel,
            **(extra or {}),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  [session] {channel} saved storage_state -> {state_path.name}", flush=True)
    except Exception as e:
        print(f"  [session] {channel} save failed: {e}", flush=True)

# ===== inlined from ctrip_hotel/completeness.py =====
REQUIRED_HOTEL = (
    "name",
    "address",
    "images",
    "score",
    "introduction",
    "facilities",
    "nearby",
)


def hotel_gaps(hotel: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    if not hotel.get("name"):
        gaps.append("name")
    if not hotel.get("address"):
        gaps.append("address")
    if not hotel.get("images"):
        gaps.append("images")
    if hotel.get("score") is None and hotel.get("review_count") is None:
        gaps.append("score/review_count")
    if not hotel.get("introduction"):
        gaps.append("introduction")
    fac = hotel.get("facilities") or []
    feat = hotel.get("features") or []
    if not fac and not feat:
        gaps.append("facilities/features")
    nearby = hotel.get("nearby") or {}
    if not any(nearby.get(k) for k in ("metro", "airport", "train", "other")):
        gaps.append("nearby")
    return gaps


def room_gaps(rooms: list[dict[str, Any]]) -> list[str]:
    if not rooms:
        return ["rooms"]
    gaps = []
    no_img = sum(1 for r in rooms if not r.get("images"))
    # detail_categories may be synthesized; also accept basic attrs as "has detail"
    no_cat = sum(
        1
        for r in rooms
        if not r.get("detail_categories")
        and not (r.get("bed") or r.get("area") or r.get("window"))
    )
    if no_img:
        gaps.append(f"rooms_without_images:{no_img}")
    if no_cat:
        gaps.append(f"rooms_without_detail:{no_cat}")
    return gaps


def document_gaps(doc: dict[str, Any]) -> list[str]:
    return hotel_gaps(doc.get("hotel") or {}) + room_gaps(doc.get("rooms") or [])


def is_complete(doc: dict[str, Any]) -> bool:
    # allow soft gaps on introduction/features if everything else ok? User said 补满 — require all.
    return not document_gaps(doc)

# ===== inlined from ctrip_hotel/warmup_gate.py =====
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

# ===== inlined from ctrip_hotel/hotel_parse.py =====
def _clean_img(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    if "np-pic.png" in url or "placeholder" in url.lower():
        return None
    if "viewall" in url.lower():
        return None
    return url


def images_from_album(album: dict[str, Any] | None) -> tuple[list[str], int | None]:
    if not album:
        return [], None
    data = album.get("data") or album
    urls: list[str] = []
    seen: set[str] = set()
    total = None

    top = data.get("hotelTopImage") or {}
    if isinstance(top, dict):
        if top.get("total"):
            total = int(top["total"])
        for item in top.get("imgUrlList") or []:
            if not isinstance(item, dict):
                continue
            u = _clean_img(item.get("imgUrl"))
            # sometimes first is placeholder; dig diffPositionUrls
            if not u:
                for d in item.get("diffPositionUrls") or []:
                    u = _clean_img(d.get("picUrl"))
                    if u:
                        break
            if u and u not in seen:
                seen.add(u)
                urls.append(u)

    pop = data.get("hotelImagePop") or {}
    provide = (pop.get("hotelProvide") or {}) if isinstance(pop, dict) else {}
    for tab in provide.get("imgTabs") or []:
        if not isinstance(tab, dict):
            continue
        if tab.get("total"):
            t = int(tab["total"])
            total = t if total is None else max(total, t)
        for block in tab.get("imgUrlList") or []:
            subs = []
            if isinstance(block, dict):
                if block.get("subImgUrlList"):
                    subs = block.get("subImgUrlList") or []
                elif block.get("link"):
                    subs = [block]
            for img in subs:
                if not isinstance(img, dict):
                    continue
                u = _clean_img(img.get("link") or img.get("imgUrl"))
                if u and u not in seen:
                    seen.add(u)
                    urls.append(u)
                if len(urls) >= 60:
                    break
        if len(urls) >= 60:
            break

    return urls, (int(total) if total is not None else len(urls))


def nearby_from_additional(additional: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"metro": [], "airport": [], "train": [], "other": []}
    if not additional:
        return out
    data = additional.get("data") or additional
    poi = (data.get("hotelPoiInfo") or {}) if isinstance(data, dict) else {}
    for group in poi.get("aroundItemList") or []:
        if not isinstance(group, dict):
            continue
        for p in group.get("poiInfoList") or []:
            if not isinstance(p, dict):
                continue
            item = {
                "name": p.get("name"),
                "distance": p.get("sinkDistanceText") or p.get("distanceDescText"),
                "tags": p.get("tagNames") or [],
            }
            tags = " ".join(str(x) for x in (item["tags"] or [])) + " " + (item["name"] or "")
            icon = str(p.get("icon") or "")
            if "地铁" in tags or "metro" in icon:
                out["metro"].append(item)
            elif "机场" in tags or "airport" in icon:
                out["airport"].append(item)
            elif "火车" in tags or "train" in icon:
                out["train"].append(item)
            else:
                if len(out["other"]) < 12:
                    out["other"].append(item)
    return out


def introduction_from_additional(additional: dict[str, Any] | None) -> str | None:
    """Build a short intro from getDetailAdditionalInfo.hotelIntroduction."""
    if not additional:
        return None
    data = additional.get("data") or additional
    intro = data.get("hotelIntroduction") or {}
    if not isinstance(intro, dict):
        return None
    parts: list[str] = []
    for card in intro.get("highLightCardList") or []:
        if not isinstance(card, dict):
            continue
        name = (card.get("tagName") or "").strip()
        desc = (card.get("tagDesc") or "").strip()
        if name and desc:
            parts.append(f"{name}：{desc}")
        elif desc:
            parts.append(desc)
        elif name:
            parts.append(name)
    for sec in intro.get("sectionList") or []:
        if not isinstance(sec, dict):
            continue
        desc = (sec.get("desc") or "").strip()
        title = (sec.get("title") or "").strip()
        if desc:
            parts.append(desc)
        elif title:
            parts.append(title)
    text = "\n".join(parts).strip()
    if len(text) < 8:
        # Fallback: facility comment snippets / tip titles
        fac = data.get("hotelFacility") or {}
        comments = ((fac.get("comment") or {}).get("commentList") or []) if isinstance(fac, dict) else []
        tips = ((data.get("hotelReservationTips") or {}).get("tipList") or [])
        bits: list[str] = []
        for c in comments[:5]:
            if isinstance(c, str) and c.strip():
                bits.append(c.strip().strip("“”\""))
            elif isinstance(c, dict):
                t = (c.get("content") or c.get("comment") or c.get("title") or "").strip()
                if t:
                    bits.append(t.strip("“”\""))
        for t in tips[:3]:
            if isinstance(t, dict) and t.get("title"):
                bits.append(str(t["title"]))
        # Policy blurbs as last resort
        if not bits:
            policy = data.get("hotelPolicy") or {}
            for item in (policy.get("policyItems") or [])[:3]:
                if not isinstance(item, dict):
                    continue
                title = (item.get("title") or item.get("policyTitle") or "").strip()
                desc = (item.get("desc") or item.get("content") or "").strip()
                if title and desc:
                    bits.append(f"{title}：{desc[:80]}")
                elif desc:
                    bits.append(desc[:120])
                elif title:
                    bits.append(title)
        text = "；".join(bits).strip()
    if len(text) < 8:
        return None
    return text[:800]


def images_from_introduction(additional: dict[str, Any] | None) -> list[str]:
    """Pull pictureList from hotelIntroduction.sectionList (album URLs often stripped)."""
    if not additional:
        return []
    data = additional.get("data") or additional
    intro = data.get("hotelIntroduction") or {}
    if not isinstance(intro, dict):
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for sec in intro.get("sectionList") or []:
        if not isinstance(sec, dict):
            continue
        for u in sec.get("pictureList") or []:
            if not isinstance(u, str) or not u.startswith("http"):
                continue
            if u in seen:
                continue
            seen.add(u)
            urls.append(u)
            if len(urls) >= 24:
                return urls
    return urls


def facilities_from_additional(additional: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten hotelFacility.category into [{name, tag?}]."""
    if not additional:
        return []
    data = additional.get("data") or additional
    fac = data.get("hotelFacility") or {}
    if not isinstance(fac, dict):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cat in fac.get("category") or []:
        if not isinstance(cat, dict):
            continue
        cat_name = cat.get("categoryName") or ""
        for item in cat.get("facilityList") or []:
            if not isinstance(item, dict):
                continue
            name = (
                item.get("facilityName")
                or item.get("name")
                or item.get("showName")
                or item.get("facilityTitle")
                or item.get("facilityShowName")
                or item.get("title")
            )
            # some payloads nest the label
            if not name and isinstance(item.get("facilityInfo"), dict):
                fi = item["facilityInfo"]
                name = fi.get("name") or fi.get("facilityName") or fi.get("title")
            if not name:
                continue
            name = str(name).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            row: dict[str, Any] = {"name": name}
            if cat_name:
                row["tag"] = str(cat_name)
            out.append(row)
            if len(out) >= 60:
                return out
    # fallback: feature highlight names
    if not out:
        intro = data.get("hotelIntroduction") or {}
        for card in (intro.get("highLightCardList") or []) if isinstance(intro, dict) else []:
            if isinstance(card, dict) and card.get("tagName"):
                n = str(card["tagName"]).strip()
                if n and n not in seen:
                    seen.add(n)
                    out.append({"name": n})
    return out


def merge_hotel_full(
    *,
    base: dict[str, Any] | None,
    page_hotel: dict[str, Any] | None,
    album: dict[str, Any] | None,
    additional: dict[str, Any] | None,
) -> dict[str, Any]:
    base = dict(base or {})
    page = dict(page_hotel or {})
    album_imgs, album_total = images_from_album(album)
    api_nearby = nearby_from_additional(additional)
    page_nearby = page.get("nearby") or {}

    def pick_nearby(key: str) -> list:
        a = api_nearby.get(key) or []
        b = page_nearby.get(key) or []
        return a or b

    images = album_imgs or list(page.get("images") or [])
    if len(images) < 4:
        intro_imgs = images_from_introduction(additional)
        if intro_imgs:
            seen = set(images)
            for u in intro_imgs:
                if u not in seen:
                    seen.add(u)
                    images.append(u)
    api_intro = introduction_from_additional(additional)
    api_fac = facilities_from_additional(additional)
    page_fac = page.get("facilities") or []
    base_fac = base.get("facilities") or []
    features = page.get("features") or base.get("features") or []
    if not features and api_fac:
        features = [{"name": x["name"]} for x in api_fac[:12]]

    hotel = {
        "hotel_id": page.get("hotel_id") or base.get("hotel_id"),
        "name": page.get("name") or base.get("name"),
        "star": page.get("star") if page.get("star") is not None else base.get("star"),
        "address": page.get("address") or base.get("address"),
        "score": page.get("score") if page.get("score") is not None else base.get("score"),
        "score_label": page.get("score_label"),
        "review_count": page.get("review_count")
        if page.get("review_count") is not None
        else base.get("comment_count"),
        "review_snippet": page.get("review_snippet"),
        "images": images,
        "image_count": album_total
        or page.get("image_count")
        or len(images),
        "features": features,
        "facilities": page_fac or api_fac or base_fac,
        "introduction": page.get("introduction") or api_intro or base.get("introduction"),
        "nearby": {
            "metro": pick_nearby("metro"),
            "airport": pick_nearby("airport"),
            "train": pick_nearby("train"),
            "other": pick_nearby("other"),
        },
        "tips": None,
    }

    # reservation tips
    if additional:
        data = additional.get("data") or additional
        tips = (data.get("hotelReservationTips") or {}).get("tipList")
        if tips:
            hotel["tips"] = tips

    return hotel

# ===== inlined from ctrip_hotel/normalize.py =====
def normalize_hotels_from_list_api(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") or {}
    hotels = data.get("hotelList") or []
    rows: list[dict[str, Any]] = []
    for item in hotels:
        info = item.get("hotelInfo") or item
        summary = info.get("summary") or {}
        name_info = info.get("nameInfo") or {}
        star = info.get("hotelStar") or {}
        comment = info.get("commentInfo") or {}
        position = info.get("positionInfo") or {}
        hotel_id = summary.get("hotelId") or info.get("hotelId")
        if not hotel_id:
            continue
        rows.append(
            {
                "hotel_id": hotel_id,
                "name": name_info.get("name") or info.get("name"),
                "en_name": name_info.get("enName"),
                "star": star.get("star"),
                "score": comment.get("commentScore"),
                "comment_count": comment.get("commenterNumber")
                or comment.get("commentCount"),
                "address": position.get("address") or position.get("positionDesc"),
                "zone": position.get("zoneNames") or position.get("areaName"),
            }
        )
    return rows


def normalize_hotels_from_dom(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for c in cards:
        hid = c.get("hotel_id")
        if not hid:
            continue
        rows.append(
            {
                "hotel_id": int(hid) if str(hid).isdigit() else hid,
                "name": c.get("name"),
                "en_name": None,
                "star": c.get("star"),
                "score": c.get("score"),
                "comment_count": c.get("comment_count"),
                "address": c.get("address"),
                "zone": c.get("zone"),
            }
        )
        # keep optional list fields for later merge
    return rows


def _is_image_url(url: str) -> bool:
    u = url.lower()
    if not u.startswith("http"):
        return False
    if any(x in u for x in (".mp4", "video-preview", "/videos/", "viewall", "placeholder", "np-pic.png")):
        return False
    return True


def _img_urls(picture_info: Any) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add(u: Any) -> None:
        if not u or not isinstance(u, str):
            return
        if not _is_image_url(u) or "查看" in u:
            return
        if u in seen:
            return
        seen.add(u)
        urls.append(u)

    if isinstance(picture_info, list):
        for p in picture_info:
            if isinstance(p, dict):
                add(p.get("url") or p.get("bigPicUrl") or p.get("smallPicUrl") or p.get("imageUrl"))
            elif isinstance(p, str):
                add(p)
    return urls


def _browse_img_urls(proom: dict[str, Any]) -> list[str]:
    """High-res room gallery from roomPhotoBrowseModel.imageItemList (2026 API)."""
    model = proom.get("roomPhotoBrowseModel") or {}
    if not isinstance(model, dict):
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for it in model.get("imageItemList") or []:
        if not isinstance(it, dict):
            continue
        u = it.get("originalImageUrl") or it.get("imageUrl")
        if not u or not isinstance(u, str) or not _is_image_url(u):
            continue
        if u in seen:
            continue
        seen.add(u)
        urls.append(u)
    return urls


def _facility_item(sub: dict[str, Any]) -> dict[str, Any]:
    additions = sub.get("additionInfo") or []
    notes = []
    free = None
    for a in additions:
        if not isinstance(a, dict):
            continue
        content = a.get("infoContent") or ""
        if "免费" in content:
            free = True
        if content:
            notes.append(content)
    # Ctrip UI often shows green「免费」when freeType == 0
    if free is None and sub.get("freeType") == 0:
        free = True
    # unavailable often still listed; icon slash not in JSON — use explicit flags if any
    available = True
    if sub.get("isNormalShow") == 0:
        available = False
    return {
        "name": sub.get("title"),
        "free": free,
        "note": "；".join(notes) if notes else None,
        "available": available,
    }


def _room_from_physic(proom: dict[str, Any]) -> dict[str, Any]:
    fac = proom.get("faciltityInfo") or proom.get("facilityInfo") or {}
    categories = []
    for block in fac.get("list") or []:
        if not isinstance(block, dict):
            continue
        items = [
            _facility_item(s)
            for s in (block.get("subList") or [])
            if isinstance(s, dict) and s.get("title")
        ]
        if items:
            categories.append({"title": block.get("title"), "items": items})

    bed = (proom.get("bedInfo") or {}).get("title")
    window = (proom.get("windowInfo") or {}).get("title")
    smoke = (proom.get("smokeInfo") or {}).get("title")
    area = (proom.get("areaInfo") or {}).get("title")
    floor = (proom.get("floorInfo") or {}).get("title")
    wifi = (proom.get("wifiInfo") or {}).get("title")
    live = (proom.get("liveInfo") or {}).get("title")
    brief = []
    for f in proom.get("physicalFacilityList") or []:
        if isinstance(f, dict) and f.get("title"):
            brief.append({"icon": f.get("icon"), "title": f.get("title")})

    # 2026 inland API often omits faciltityInfo; synthesize overview from attrs.
    if not categories:
        overview = []
        for title, val in (
            ("床型", bed),
            ("窗户", window),
            ("面积", area),
            ("楼层", floor),
            ("吸烟", smoke),
            ("网络", wifi),
            ("入住", live),
        ):
            if val:
                overview.append({"name": title, "free": None, "note": str(val), "available": True})
        if not overview and proom.get("name"):
            overview.append(
                {
                    "name": "房型",
                    "free": None,
                    "note": str(proom.get("name")),
                    "available": True,
                }
            )
        if overview:
            categories.append({"title": "房型概况", "items": overview})

    images = _browse_img_urls(proom) or _img_urls(proom.get("pictureInfo"))

    return {
        "room_id": proom.get("id"),
        "room_name": proom.get("name"),
        "images": images,
        "bed": bed,
        "window": window,
        "smoke": smoke,
        "area": area,
        "floor": floor,
        "wifi": wifi,
        "extra_bed": None,
        "brief_facilities": brief,
        "detail_categories": categories,
        "offers": [],
    }


def _offer_from_sale(sroom: dict[str, Any]) -> dict[str, Any]:
    """Build an offer from a saleRoomMap entry.

    New (2026) saleRoomMap structure has no mealInfo/cancelInfo/guestCountInfo
    top-level fields — those live in tagInfoList ("2人入住", "含X份早餐",
    "免费取消"...) and serviceTagList ("立即确认"...). We scan both tag lists.
    """
    tags: list[str] = []
    for tl in (sroom.get("tagInfoList") or []) + (sroom.get("serviceTagList") or []):
        if isinstance(tl, dict) and tl.get("tagTitle"):
            tags.append(str(tl["tagTitle"]))
    meal = None
    cancel = None
    occupancy = None
    for t in tags:
        if "早餐" in t or "含早" in t or "无早" in t:
            meal = t
        elif "取消" in t or "不可退" in t:
            cancel = t
        elif "人入住" in t or "成人" in t:
            m = re.search(r"(\d+)\s*人", t)
            if m:
                occupancy = int(m.group(1))
    confirm = None
    for t in tags:
        if "确认" in t or "立即" in t:
            confirm = t
            break
    booking = sroom.get("bookingStatusInfo") or {}
    pay = sroom.get("paymentInfo") or {}
    left = booking.get("remainRoomQuantity")
    if left is not None and int(left) >= 999:
        left = None
    return {
        "offer_id": sroom.get("id"),
        "meal": meal,
        "cancel": cancel,
        "confirm": confirm,
        "pay": pay.get("subTitle") or pay.get("paymentTitleNew"),
        "occupancy": occupancy,
        "left": left,
        "price_str": sroom.get("priceStr"),
        "room_attr": sroom.get("roomAttr"),
    }


def build_rooms_from_api(api_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not api_payload:
        return []
    data = api_payload.get("data") or {}
    if data.get("htlSpiderActionErrorCode"):
        return []
    physic = data.get("physicRoomMap") or {}
    sale = data.get("saleRoomMap") or {}
    if not isinstance(physic, dict):
        return []

    rooms_by_id: dict[Any, dict[str, Any]] = {}
    for pid, proom in physic.items():
        if not isinstance(proom, dict):
            continue
        room = _room_from_physic(proom)
        rid = room["room_id"] or pid
        room["room_id"] = rid
        rooms_by_id[str(rid)] = room

    if isinstance(sale, dict):
        for sroom in sale.values():
            if not isinstance(sroom, dict):
                continue
            phys_id = sroom.get("physicalRoomId")
            key = str(phys_id) if phys_id is not None else None
            if not key or key not in rooms_by_id:
                continue
            rooms_by_id[key]["offers"].append(_offer_from_sale(sroom))

    # dedupe offers inside room
    out = []
    for room in rooms_by_id.values():
        seen = set()
        uniq_offers = []
        for o in room["offers"]:
            sig = (o.get("meal"), o.get("cancel"), o.get("confirm"), o.get("pay"), o.get("occupancy"))
            if sig in seen:
                continue
            seen.add(sig)
            uniq_offers.append(o)
        room["offers"] = uniq_offers
        out.append(room)

    out.sort(key=lambda r: str(r.get("room_name") or ""))
    return out


def merge_hotel_info(
    base: dict[str, Any] | None,
    page_hotel: dict[str, Any] | None,
) -> dict[str, Any]:
    base = dict(base or {})
    page = dict(page_hotel or {})
    images = list(page.get("images") or [])
    hotel = {
        "hotel_id": page.get("hotel_id") or base.get("hotel_id"),
        "name": page.get("name") or base.get("name"),
        "star": page.get("star") if page.get("star") is not None else base.get("star"),
        "address": page.get("address") or base.get("address"),
        "score": page.get("score") if page.get("score") is not None else base.get("score"),
        "score_label": page.get("score_label"),
        "review_count": page.get("review_count")
        if page.get("review_count") is not None
        else base.get("comment_count"),
        "review_snippet": page.get("review_snippet"),
        "images": images,
        "image_count": page.get("image_count") or len(images),
        "features": page.get("features") or [],
        "facilities": page.get("facilities") or [],
        "introduction": page.get("introduction"),
        "nearby": page.get("nearby")
        or {"metro": [], "airport": [], "train": []},
    }
    return hotel


def fill_hotel_images_from_rooms(doc: dict[str, Any]) -> dict[str, Any]:
    """If hotel gallery thin/empty, assemble covers from room photos (deduped)."""
    hotel = doc.get("hotel") or {}
    existing = list(hotel.get("images") or [])
    # Upgrade when album only left a video cover / single thumb.
    if len(existing) >= 8:
        return doc
    seen: set[str] = set(existing)
    imgs: list[str] = list(existing)
    for room in doc.get("rooms") or []:
        for u in room.get("images") or []:
            if u in seen:
                continue
            seen.add(u)
            imgs.append(u)
            if len(imgs) >= 24:
                break
        if len(imgs) >= 24:
            break
    if imgs:
        hotel["images"] = imgs
        hotel["image_count"] = max(int(hotel.get("image_count") or 0), len(imgs))
        doc["hotel"] = hotel
    return doc


def _min_price_from_api(api_payload: dict[str, Any] | None) -> float | None:
    if not api_payload:
        return None
    data = api_payload.get("data") or api_payload
    if not isinstance(data, dict):
        return None
    bar = data.get("hotelDetailBarInfo") or {}
    raw = bar.get("price")
    if raw is None or raw == "":
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def build_hotel_document(
    *,
    hotel_meta: dict[str, Any],
    page_hotel: dict[str, Any] | None,
    fetch_result: dict[str, Any],
    check_in: str,
    check_out: str,
) -> dict[str, Any]:
    api = fetch_result.get("api") if isinstance(fetch_result, dict) else None
    rooms = build_rooms_from_api(api)
    # fallback: thin DOM rooms if API empty
    if not rooms:
        for dr in fetch_result.get("dom_rooms") or []:
            rooms.append(
                {
                    "room_id": None,
                    "room_name": dr.get("room_name"),
                    "images": dr.get("images") or [],
                    "bed": dr.get("bed"),
                    "window": dr.get("window"),
                    "smoke": dr.get("smoke"),
                    "area": dr.get("area"),
                    "floor": dr.get("floor"),
                    "wifi": dr.get("wifi"),
                    "extra_bed": None,
                    "brief_facilities": [],
                    "detail_categories": [],
                    "offers": dr.get("sales")
                    or [
                        {
                            "meal": None,
                            "cancel": None,
                            "confirm": None,
                            "pay": None,
                            "occupancy": None,
                            "left": None,
                        }
                    ],
                }
            )

    hotel = merge_hotel_full(
        base=hotel_meta,
        page_hotel=page_hotel,
        album=fetch_result.get("album") if isinstance(fetch_result, dict) else None,
        additional=fetch_result.get("additional")
        if isinstance(fetch_result, dict)
        else None,
    )
    min_price = _min_price_from_api(api)
    if min_price is not None:
        hotel["min_price"] = min_price
        hotel["min_price_currency"] = "CNY"
    doc = {
        "hotel_id": hotel.get("hotel_id") or hotel_meta.get("hotel_id"),
        "check_in": check_in,
        "check_out": check_out,
        "source": fetch_result.get("source"),
        "hotel": hotel,
        "rooms": rooms,
    }
    return fill_hotel_images_from_rooms(doc)


# Backward-compatible helpers used by diagnose
def normalize_rooms(
    hotel_id: int | str,
    result: dict[str, Any],
    *,
    check_in: str,
    check_out: str,
) -> list[dict[str, Any]]:
    doc = build_hotel_document(
        hotel_meta={"hotel_id": hotel_id},
        page_hotel=result.get("page_hotel"),
        fetch_result=result,
        check_in=check_in,
        check_out=check_out,
    )
    flat = []
    for room in doc["rooms"]:
        for offer in room.get("offers") or [None]:
            flat.append(
                {
                    "hotel_id": hotel_id,
                    "room_id": room.get("room_id"),
                    "room_name": room.get("room_name"),
                    "bed": room.get("bed"),
                    "window": room.get("window"),
                    "images": len(room.get("images") or []),
                    "meal": (offer or {}).get("meal"),
                    "cancel": (offer or {}).get("cancel"),
                    "occupancy": (offer or {}).get("occupancy"),
                    "error": None if room.get("room_name") else "no_room_data",
                }
            )
    return flat or [
        {
            "hotel_id": hotel_id,
            "check_in": check_in,
            "check_out": check_out,
            "error": "no_room_data",
            "room_name": None,
        }
    ]

# ===== inlined from ctrip_hotel/api_client.py =====
# ---------------------------------------------------------------------------
# Hotel list — pure HTTP (no browser needed)
# ---------------------------------------------------------------------------

_LIST_URL = "https://m.ctrip.com/restapi/soa2/31454/fetchHotelList"
_ROOM_URL = "https://m.ctrip.com/restapi/soa2/33278/getHotelRoomListInland"


def _domestic_room_ok(payload: dict[str, Any] | None, *, token: str = "") -> tuple[bool, str]:
    """Return (ok, detail) for a getHotelRoomListInland payload."""
    reason, detail = classify_domestic_probe(payload, token=token)
    return reason == "ok", detail


def extract_proxy_pool(cfg: dict[str, Any]) -> list[str]:
    """Build a list of proxy URLs from config.

    Sources (first non-empty wins):
      1. `proxy_list` (list of "ip:port" / "http://ip:port")
      2. `proxy` (single)
      3. `proxy_api_url` — a JSON API that returns {"obj": [{"ip":..,"port":..}]}
    """
    out: list[str] = []
    for p in cfg.get("proxy_list") or []:
        s = str(p).strip()
        if s and s not in out:
            out.append(s if "://" in s else f"http://{s}")
    single = cfg.get("proxy")
    if single:
        s = str(single).strip()
        if s and s not in out:
            out.append(s if "://" in s else f"http://{s}")
    api_url = cfg.get("proxy_api_url")
    if api_url and not out:
        try:
            import urllib.request
            raw = urllib.request.urlopen(str(api_url), timeout=15).read()
            data = json.loads(raw)
            for item in data.get("obj") or []:
                ip, port = item.get("ip"), item.get("port")
                if ip and port:
                    u = f"http://{ip}:{port}"
                    if u not in out:
                        out.append(u)
        except Exception as e:
            print(f"  [proxy] 提取代理失败: {e}")
    return out

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

_LIST_HEAD = {
    "platform": "H5",
    "group": "ctrip",
    "cid": "hotel",
    "ctok": "",
    "cver": "1.0",
    "lang": "01",
    "sid": "0000",
    "syscode": "09",
    "auth": "",
    "extension": [],
}


def fetch_hotel_list_pure(
    *,
    city_id: int,
    check_in: str,
    check_out: str,
    pages: int = 1,
    page_size: int = 20,
    delay_ms: int = 0,
    on_page: Any = None,
    proxy: str | None = None,
    proxies: list[str] | None = None,
    max_items: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch hotel catalog via pure HTTP (curl_cffi impersonate=chrome).

    `proxy` is a single proxy URL like "http://ip:port".
    `proxies` is a list of proxy URLs; requests rotate through them per page.
    `max_items`: stop paging once enough raw list items are collected.
    """
    headers = {
        "User-Agent": MOBILE_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/json",
        "Referer": "https://m.ctrip.com/webapp/hotel/hotellist",
        "Origin": "https://m.ctrip.com",
    }
    proxy_pool = list(proxies or [])
    if proxy:
        proxy_pool.append(proxy)
    all_items: list[dict[str, Any]] = []
    t_list = time.time()
    need = int(max_items) if max_items else 0
    for page_idx in range(1, int(pages or 1) + 1):
        body = {
            "head": _LIST_HEAD,
            "destination": {"type": 2, "geo": {"cityId": int(city_id)}},
            "checkIn": check_in,
            "checkOut": check_out,
            "paging": {"pageIndex": page_idx, "pageSize": int(page_size)},
        }
        proxy_url = proxy_pool[(page_idx - 1) % len(proxy_pool)] if proxy_pool else None
        try:
            proxies = (
                {"http": proxy_url, "https": proxy_url} if proxy_url else None
            )
            r = session_post(
                _LIST_URL,
                json=body,
                headers=headers,
                proxies=proxies,
                impersonate="chrome",
                timeout=20,
                tag="api_list",
            )
            j = r.json()
        except Exception as e:
            print(f"  [list] page {page_idx} error: {e}")
            break
        data = j.get("data") or {}
        items = data.get("hotelList") or []
        if not items:
            break
        all_items.extend(items)
        if on_page:
            on_page(items, page_idx)
        if need and len(all_items) >= need:
            break
        if page_idx < int(pages or 1) and delay_ms:
            time.sleep(delay_ms / 1000.0)
        # Ctrip caps pageSize ~20; stop if fewer than requested (last page).
        if len(items) < int(page_size):
            break
    print(
        f"  [list] {len(all_items)} items / {page_idx} page(s) "
        f"{time.time() - t_list:.1f}s",
        flush=True,
    )
    return all_items


# ---------------------------------------------------------------------------
# Room status — single headless browser page + in-page fetch replay
# ---------------------------------------------------------------------------

_ROOM_TEMPLATE_SCRIPT = """
() => {
  const tpl = window.__CTRIP_ROOM_TEMPLATE__;
  if (!tpl) return null;
  return {
    url: tpl.url,
    post: tpl.post,
    hasTemplate: true,
  };
}
"""

_FETCH_ROOM_SCRIPT = """
async (args) => {
  const {url, post, hotelId} = args;
  const resp = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: post,
    credentials: 'include',
  });
  const j = await resp.json();
  return j;
}
"""


class ApiRoomClient:
    """One headless browser page that produces valid phantom-tokens.

    Use `fetch_room(hotel_id)` repeatedly (serial) to get full room status.
    """

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        worker_id: int | None = None,
        seed_hotel_id: int | str | None = None,
    ) -> None:
        self.cfg = cfg
        self.worker_id = worker_id
        self.seed_hotel_id = seed_hotel_id
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._template: dict[str, Any] | None = None
        self._album_template: dict[str, Any] | None = None
        self._additional_template: dict[str, Any] | None = None
        self._cookies: str = ""
        self._ready: bool = False
        self._ready_detail: str = ""
        self._session_channel = "api_domestic"

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "ApiRoomClient":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        # API mode is headless by default; set api_headed=true (or pass
        # --headed) only to manually pass a one-time captcha during warmup.
        headed = bool(self.cfg.get("api_headed", self.cfg.get("headed", False)))
        launch_kwargs: dict[str, Any] = {
            "headless": not headed,
            "args": [
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-background-networking",
            ],
        }
        # Prefer bundled Chromium for domestic API so it does not fight headed
        # Edge used by intl signer (intl_browser_channel / browser_channel).
        channel = (
            self.cfg.get("api_browser_channel")
            if "api_browser_channel" in self.cfg
            else ""
        )
        if channel is None:
            channel = ""
        channel = str(channel).lower()
        if channel in {"msedge", "chrome"}:
            try:
                self._browser = self._pw.chromium.launch(
                    channel=channel, **launch_kwargs
                )
            except Exception:
                self._browser = self._pw.chromium.launch(**launch_kwargs)
        else:
            self._browser = self._pw.chromium.launch(**launch_kwargs)
        # H5 detail page fires album/additional more reliably on a phone UA.
        context_kwargs: dict[str, Any] = {
            "locale": "zh-CN",
            "viewport": {"width": 390, "height": 844},
            "user_agent": MOBILE_UA,
            "is_mobile": True,
            "has_touch": True,
        }
        proxy = self.cfg.get("proxy") or self.cfg.get("browser_proxy")
        if proxy:
            context_kwargs["proxy"] = {"server": str(proxy)}
        # Reuse cookies/localStorage from last successful run (Playwright storage_state).
        max_age = int(self.cfg.get("session_max_age_sec") or 6 * 3600)
        state_path = load_storage_state(self._session_channel, max_age_sec=max_age)
        if state_path is not None:
            context_kwargs["storage_state"] = str(state_path)
        ctx = self._browser.new_context(**context_kwargs)
        self._context = ctx
        self._page = ctx.new_page()
        self._page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = window.chrome || { runtime: {} };
            """
        )
        # Capture template, then LIVE probe — batch must not start until probe passes.
        self._warmup()
        self._ensure_ready()
        return self

    def is_ready(self) -> bool:
        return bool(self._ready and self._template)

    def _probe_seed_ids(self) -> list[Any]:
        seeds: list[Any] = []
        if self.seed_hotel_id is not None:
            seeds.append(self.seed_hotel_id)
        cfg_seed = self.cfg.get("seed_hotel_id")
        if cfg_seed:
            seeds.append(cfg_seed)
        for x in self.cfg.get("seed_hotel_ids") or []:
            if str(x).isdigit():
                seeds.append(int(x))
        try:
            body = json.loads((self._template or {}).get("post") or "{}")
            hid = (body.get("search") or {}).get("hotelId")
            if hid:
                seeds.insert(0, hid)
        except Exception:
            pass
        # dedupe preserve order
        out: list[Any] = []
        seen: set[str] = set()
        for s in seeds:
            k = str(s)
            if k not in seen:
                seen.add(k)
                out.append(s)
        return out or [1]

    def _ensure_ready(self) -> None:
        """Hard gate: diagnose → auto-fix → only then allow batch."""
        if not self._template:
            raise final_probe_error(
                channel="api",
                reason="template_missing",
                detail="room template missing",
                hotel_id=self.seed_hotel_id,
                tried=[],
            )

        seeds = self._probe_seed_ids()
        seed_idx = 0
        probe_id = seeds[seed_idx]
        attempts = max(int(self.cfg.get("warmup_probe_retries") or 4), 1)
        last_reason, last_detail = "empty", ""
        tried: list[str] = []

        for i in range(attempts):
            token = (self._template.get("headers") or {}).get("phantom-token") or ""
            if not str(token).strip():
                reason, detail = "empty_token", "phantom-token empty"
                _, action, hint = DOMESTIC_PLAYBOOK[reason]
                print_probe_diagnosis(
                    channel="api",
                    reason=reason,
                    detail=detail,
                    action=f"执行 {action}: 重新捕获页面模板",
                    hint=hint,
                    attempt=i + 1,
                    attempts=attempts,
                    hotel_id=probe_id,
                )
                tried.append(f"{reason}:recapture")
                last_reason, last_detail = reason, detail
                if self._page is None:
                    break
                try:
                    self.seed_hotel_id = seeds[min(seed_idx, len(seeds) - 1)]
                    self._warmup()
                except Exception as e:
                    last_detail = f"{detail}; recapture={e}"
                continue

            payload = self.fetch_room(probe_id)
            reason, detail = classify_domestic_probe(payload, token=token)
            if reason == "ok":
                self._ready = True
                self._ready_detail = detail
                print(
                    f"  [api] probe ok ({i + 1}/{attempts}): hotel={probe_id} {detail}",
                    flush=True,
                )
                # Persist browser state for next run (hybrid: browser once tax amortized).
                if self._context is not None:
                    save_storage_state(
                        self._session_channel,
                        self._context,
                        extra={"probe_hotel": probe_id, "detail": detail},
                    )
                if self._album_template and self._additional_template:
                    self.release_browser()
                return

            _, action, hint = DOMESTIC_PLAYBOOK.get(
                reason, (reason, "backoff", "查看日志")
            )
            fix_desc = {
                "recapture": "重新预热捕获 token",
                "next_seed": f"换探针酒店 → {seeds[min(seed_idx + 1, len(seeds) - 1)]}",
                "backoff": "短暂退避后重试",
            }.get(action, action)
            print_probe_diagnosis(
                channel="api",
                reason=reason,
                detail=detail,
                action=f"执行 {action}: {fix_desc}",
                hint=hint,
                attempt=i + 1,
                attempts=attempts,
                hotel_id=probe_id,
            )
            last_reason, last_detail = reason, detail

            if action == "next_seed" and seed_idx + 1 < len(seeds):
                seed_idx += 1
                probe_id = seeds[seed_idx]
                tried.append(f"{reason}:next_seed:{probe_id}")
                continue
            if action == "recapture":
                tried.append(f"{reason}:recapture")
                if self._page is None:
                    break
                try:
                    if seed_idx + 1 < len(seeds):
                        seed_idx += 1
                        self.seed_hotel_id = seeds[seed_idx]
                        probe_id = seeds[seed_idx]
                    self._warmup()
                except Exception as e:
                    last_detail = f"{detail}; recapture={e}"
                continue
            # backoff
            tried.append(f"{reason}:backoff")
            time.sleep(0.5 * (i + 1))

        self._ready = False
        raise final_probe_error(
            channel="api",
            reason=last_reason,
            detail=last_detail,
            hotel_id=probe_id,
            tried=tried,
        )

    # -- warmup ------------------------------------------------------------

    def _warmup(self) -> None:
        """Navigate to a hotel detail page so the page JS emits a room request.

        We capture the request (URL + POST body) that the page fires, and stash it
        on window.__CTRIP_ROOM_TEMPLATE__ for reuse.
        """
        assert self._page is not None
        seed = self.seed_hotel_id
        if seed is None:
            seed = int(self.cfg.get("seed_hotel_id") or 0) or 1
        # Try a few seed hotels in case the page does not fire the room API
        # (anti-bot can be flaky). Reuses the same page across attempts.
        seed_ids = [seed]
        extras = self.cfg.get("seed_hotel_ids") or []
        seed_ids.extend(int(x) for x in extras if str(x).isdigit())

        captured: dict[str, Any] = {}

        # Capture via fetch hook + Playwright request events (XHR may bypass fetch).
        self._page.add_init_script(
            """
            window.__CTRIP_CAPTURED__ = window.__CTRIP_CAPTURED__ || {};
            const origFetch = window.fetch;
            window.fetch = function(...args) {
                try {
                    const [url, opts] = args;
                    const u = String(url);
                    const post = opts ? String(opts.body || '') : '';
                    let headers = {};
                    try {
                        if (opts && opts.headers) headers = JSON.parse(JSON.stringify(opts.headers));
                    } catch (e) {}
                    if (u.includes('getHotelRoomListInland')) {
                        window.__CTRIP_CAPTURED__.room = { url: u, post, headers };
                    } else if (u.toLowerCase().includes('gethotelalbumpicture') || u.toLowerCase().includes('ctgethotelalbum')) {
                        if (!window.__CTRIP_CAPTURED__.album) {
                            window.__CTRIP_CAPTURED__.album = { url: u, post, headers };
                        }
                    } else if (u.toLowerCase().includes('getdetailadditionalinfo')) {
                        if (!window.__CTRIP_CAPTURED__.additional) {
                            window.__CTRIP_CAPTURED__.additional = { url: u, post, headers };
                        }
                    }
                } catch(e) {}
                return origFetch.apply(this, args);
            };
            """
        )

        def _on_request(req) -> None:
            u = req.url or ""
            ul = u.lower()
            try:
                post = req.post_data or ""
            except Exception:
                post = ""
            if "gethotelroomlistinland" in ul and "room" not in captured:
                captured["room"] = {
                    "url": u,
                    "post": post,
                    "headers": dict(req.headers),
                }
            elif (
                "gethotelalbumpicture" in ul or "ctgethotelalbum" in ul
            ) and "album" not in captured:
                captured["album"] = {"url": u, "post": post}
            elif "getdetailadditionalinfo" in ul and "additional" not in captured:
                captured["additional"] = {"url": u, "post": post}

        self._page.on("request", _on_request)

        def _block_heavy(route) -> None:
            try:
                rtype = (route.request.resource_type or "").lower()
                if rtype in {"image", "media", "font", "stylesheet", "texttrack"}:
                    route.abort()
                else:
                    route.continue_()
            except Exception:
                # Route may already be settled when page closes mid-warmup.
                pass

        try:
            self._page.route("**/*", _block_heavy)
        except Exception:
            pass

        t_warm = time.time()
        for i, sid in enumerate(seed_ids):
            url = (
                f"https://m.ctrip.com/webapp/hotel/hoteldetail/{sid}.html"
                f"?checkIn={self.cfg['check_in']}&checkOut={self.cfg['check_out']}"
            )
            print(f"  [api] warmup attempt {i + 1}/{len(seed_ids)}: seed={sid}")
            # Keep network-captured room if any; clear JS bag for this navigation.
            try:
                self._page.evaluate("() => { window.__CTRIP_CAPTURED__ = {}; }")
            except Exception:
                pass
            try:
                self._page.goto(url, timeout=45000, wait_until="domcontentloaded")
            except Exception as e:
                print(f"  [warmup] goto warning: {e}")
            # Wait for room API; keep scrolling so album/additional lazy-load too.
            deadline = time.time() + 16
            while time.time() < deadline:
                try:
                    cap = self._page.evaluate("() => window.__CTRIP_CAPTURED__ || {}")
                except Exception:
                    cap = {}
                if cap.get("room") and "room" not in captured:
                    captured["room"] = cap["room"]
                if cap.get("album"):
                    captured["album"] = cap["album"]
                if cap.get("additional"):
                    captured["additional"] = cap["additional"]
                if captured.get("room") and captured.get("additional"):
                    # album is optional — room browse images usually cover gaps
                    break
                self._page.wait_for_timeout(200)
                if time.time() < deadline:
                    try:
                        self._page.mouse.wheel(0, 1800)
                    except Exception:
                        pass
            if "room" in captured:
                # Extra scroll window for additional if still missing.
                if "additional" not in captured:
                    extra_deadline = time.time() + 6
                    while time.time() < extra_deadline and "additional" not in captured:
                        try:
                            cap = self._page.evaluate(
                                "() => window.__CTRIP_CAPTURED__ || {}"
                            )
                        except Exception:
                            cap = {}
                        if cap.get("additional"):
                            captured["additional"] = cap["additional"]
                        if cap.get("album"):
                            captured["album"] = cap["album"]
                        self._page.wait_for_timeout(200)
                        try:
                            self._page.mouse.wheel(0, 1800)
                        except Exception:
                            pass
                break
        print(f"  [api] capture phase {time.time() - t_warm:.1f}s", flush=True)

        if "room" not in captured:
            raise RuntimeError(
                "warmup failed: 未捕获到房态请求模板。常见原因：当前网络/IP 触发临时风控。"
                "可稍等几分钟重试，或设置 headed=true 手动打开窗口过一次人机验证，"
                "或换 seed_hotel_id / seed_hotel_ids。"
            )
        # Stash templates on window for later evaluate() calls.
        self._page.evaluate(
            """(tpl) => { window.__CTRIP_ROOM_TEMPLATE__ = tpl; }""",
            {"url": captured["room"]["url"], "post": captured["room"]["post"]},
        )
        self._template = captured["room"]
        self._album_template = captured.get("album")
        self._additional_template = captured.get("additional")
        # Capture cookies from the browser context for pure-HTTP replays.
        try:
            self._cookies = "; ".join(
                f"{c['name']}={c['value']}" for c in self._page.context.cookies()
            )
        except Exception:
            self._cookies = ""
        print(
            f"  [api] warmup capture ok: room={'Y' if self._template else 'N'} "
            f"album={'Y' if self._album_template else 'N'} "
            f"additional={'Y' if self._additional_template else 'N'}"
        )
        # Browser is released only after _ensure_ready() probe passes.

    def release_browser(self) -> None:
        """Close Playwright; keep captured templates/cookies for HTTP reuse."""
        # Unroute first — closing with active route handlers spams CancelledError.
        try:
            if self._page is not None:
                self._page.unroute("**/*")
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        self._browser = None
        self._context = None
        self._page = None
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None
        ev = self.cfg.get("_api_browser_released")
        if ev is not None:
            try:
                ev.set()
            except Exception:
                pass

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release_browser()

    def _room_headers(self) -> dict[str, str]:
        """Build headers for pure-HTTP room requests from the captured template."""
        hdrs = self._template.get("headers") or {}
        base = {
            "User-Agent": hdrs.get("user-agent")
            or "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json",
            "Referer": hdrs.get("referer") or "https://m.ctrip.com/webapp/hotel/hoteldetail",
            "Origin": "https://m.ctrip.com",
            "phantom-token": hdrs.get("phantom-token") or "",
            "cookie": self._cookies or "",
        }
        # Carry the anti-bot context headers that the page sets.
        for k in (
            "x-ctx-wclient-req",
            "x-ctx-ubt-pageid",
            "x-ctx-ubt-vid",
            "x-ctx-ubt-sid",
            "x-ctx-ubt-pvid",
            "cookieorigin",
            "w-payload-source",
        ):
            v = hdrs.get(k)
            if v:
                base[k] = v
        return base

    def fetch_room(self, hotel_id: int | str) -> dict[str, Any]:
        """Fetch full room status for one hotel via pure HTTP (token reuse)."""
        if self._template is None:
            raise RuntimeError("client not warmed up: room template missing")
        try:
            body = json.loads(self._template["post"])
            body["search"]["hotelId"] = int(hotel_id)
        except Exception:
            body = {"search": {"hotelId": int(hotel_id)}}
        try:
            r = session_post(
                self._template["url"],
                json=body,
                headers=self._room_headers(),
                impersonate="chrome",
                timeout=20,
                tag="api_room",
            )
            j = r.json()
        except Exception as e:
            return {"error": str(e), "data": {}}
        if not isinstance(j, dict):
            return {"error": "non-json response", "data": {}}
        return j

    def fetch_room_batch(
        self,
        hotel_ids: list[int | str],
        *,
        max_workers: int = 8,
    ) -> list[dict[str, Any]]:
        """Fetch rooms for many hotels with a thread pool (pure HTTP)."""
        if not self.is_ready():
            raise RuntimeError(
                "拒绝批量抓取：API 预热探针未通过 "
                f"(ready={self._ready}, detail={self._ready_detail!r})"
            )
        if max_workers <= 1:
            out = []
            for hid in hotel_ids:
                try:
                    out.append(self.fetch_room(hid))
                except Exception as e:
                    out.append({"error": str(e), "data": {}})
                time.sleep(float(self.cfg.get("delay_ms") or 0) / 1000.0)
            return out

        results: dict[int | str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(self.fetch_room, hid): hid for hid in hotel_ids}
            for fut in as_completed(futs):
                hid = futs[fut]
                try:
                    results[hid] = fut.result()
                except Exception as e:
                    results[hid] = {"error": str(e), "data": {}}
        return [results[hid] for hid in hotel_ids]

    # -- album / additional -------------------------------------------------

    def _enrich_body(self, kind: str, template: dict[str, Any], hotel_id: int | str) -> str:
        post = template.get("post") or ""
        try:
            body = json.loads(post)
            if kind == "album":
                if "HotelID" in body:
                    body["HotelID"] = int(hotel_id)
                elif "hotelId" in body:
                    body["hotelId"] = int(hotel_id)
            elif kind == "additional":
                if body.get("queryInfo") is not None:
                    body["queryInfo"]["hotelId"] = int(hotel_id)
            return json.dumps(body)
        except Exception:
            return post

    def _fetch_enrich_http(
        self, kind: str, template: dict[str, Any], hotel_id: int | str
    ) -> dict[str, Any]:
        """Pure-HTTP album/additional using room session phantom-token + cookies."""
        url = template.get("url") or ""
        if not url:
            return {}
        payload = self._enrich_body(kind, template, hotel_id)
        try:
            r = session_post(
                url,
                data=payload.encode("utf-8") if isinstance(payload, str) else payload,
                headers=self._room_headers(),
                impersonate="chrome",
                timeout=20,
                tag="api_enrich",
            )
            j = r.json()
        except Exception as e:
            return {"error": str(e)}
        return j if isinstance(j, dict) else {}

    def fetch_album(self, hotel_id: int | str) -> dict[str, Any]:
        """Fetch hotel gallery; prefer HTTP, fall back to in-page replay."""
        if not self._album_template:
            return {}
        http = self._fetch_enrich_http("album", self._album_template, hotel_id)
        if http and not http.get("error") and (
            http.get("data") or http.get("AlbumCategoryList") or http.get("ResponseStatus")
        ):
            return http
        if self._page is None:
            return http or {}
        return self._replay("album", self._album_template, hotel_id)

    def fetch_additional(self, hotel_id: int | str) -> dict[str, Any]:
        """Fetch POI/nearby; prefer HTTP, fall back to in-page replay."""
        if not self._additional_template:
            return {}
        http = self._fetch_enrich_http("additional", self._additional_template, hotel_id)
        if http and not http.get("error") and (
            http.get("data") or http.get("ResponseStatus")
        ):
            return http
        if self._page is None:
            return http or {}
        return self._replay("additional", self._additional_template, hotel_id)

    def fetch_enrich_batch(
        self,
        hotel_ids: list[int | str],
        *,
        max_workers: int = 8,
    ) -> dict[Any, tuple[dict[str, Any], dict[str, Any]]]:
        """Parallel album+additional via HTTP (stable fallback to serial page replay)."""
        out: dict[Any, tuple[dict[str, Any], dict[str, Any]]] = {
            hid: ({}, {}) for hid in hotel_ids
        }
        if not self._album_template and not self._additional_template:
            return out

        def _http_ok_album(p: dict[str, Any]) -> bool:
            return bool(p) and not p.get("error") and bool(
                p.get("data") or p.get("AlbumCategoryList")
            )

        def _http_ok_add(p: dict[str, Any]) -> bool:
            return bool(p) and not p.get("error") and bool(
                p.get("data") or p.get("ResponseStatus")
            )

        def _one(hid: int | str) -> tuple[Any, dict[str, Any], dict[str, Any]]:
            album: dict[str, Any] = {}
            add: dict[str, Any] = {}
            if self._album_template:
                album = self._fetch_enrich_http("album", self._album_template, hid)
            if self._additional_template:
                add = self._fetch_enrich_http(
                    "additional", self._additional_template, hid
                )
            return hid, album, add


        workers = max(min(max_workers, len(hotel_ids) or 1), 1)
        http_results: dict[Any, tuple[dict[str, Any], dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_one, hid) for hid in hotel_ids]
            for fut in as_completed(futs):
                hid, album, add = fut.result()
                http_results[hid] = (album, add)

        # Serial page fallback for HTTP misses (Playwright page is not thread-safe).
        fallback_n = 0
        for hid in hotel_ids:
            album, add = http_results.get(hid, ({}, {}))
            if self._album_template and not _http_ok_album(album) and self._page:
                album = self._replay("album", self._album_template, hid)
                fallback_n += 1
            if self._additional_template and not _http_ok_add(add) and self._page:
                add = self._replay("additional", self._additional_template, hid)
                fallback_n += 1
            out[hid] = (
                album if isinstance(album, dict) else {},
                add if isinstance(add, dict) else {},
            )
        if fallback_n:
            print(f"  [api] enrich page-fallback calls={fallback_n}", flush=True)
        return out

    def _replay(
        self,
        kind: str,
        template: dict[str, Any],
        hotel_id: int | str,
    ) -> dict[str, Any]:
        """Replay a captured request inside the page, changing only hotelId."""
        payload = self._enrich_body(kind, template, hotel_id)
        try:
            j = self._page.evaluate(
                """async (args) => {
                    const resp = await fetch(args.url, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: args.post,
                        credentials: 'include',
                    });
                    try { return await resp.json(); }
                    catch(e) { return {error: String(e)}; }
                }""",
                {"url": template["url"], "post": payload},
            )
        except Exception as e:
            return {"error": str(e)}
        return j if isinstance(j, dict) else {}


def normalize_list_payloads(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert raw hotelList items into the catalog shape the rest of the code uses.

    Mirrors `normalize_hotels_from_list_api` but works directly on the raw items.
    """
    rows: list[dict[str, Any]] = []
    for item in items:
        info = item.get("hotelInfo") or item
        summary = info.get("summary") or {}
        name_info = info.get("nameInfo") or {}
        star = info.get("hotelStar") or {}
        comment = info.get("commentInfo") or {}
        position = info.get("positionInfo") or {}
        hotel_id = summary.get("hotelId") or info.get("hotelId")
        if not hotel_id:
            continue
        rows.append(
            {
                "hotel_id": hotel_id,
                "name": name_info.get("name") or info.get("name"),
                "en_name": name_info.get("enName"),
                "star": star.get("star"),
                "score": comment.get("commentScore"),
                "comment_count": comment.get("commenterNumber")
                or comment.get("commentCount"),
                "address": position.get("address") or position.get("positionDesc"),
                "zone": position.get("zoneNames") or position.get("areaName"),
            }
        )
    return rows


def images_from_album_picture(album_payload: dict[str, Any]) -> list[str]:
    """Extract image URLs from the `getHotelAlbumPicture` response.

    Structure: data.AlbumCategoryList[].PictureCategoryList[].AlbumPictureList[]
    Each picture has LargeUrl / SmallUrl / OtherUrl / VideoImageUrl.
    2026+: many official slots are video-only (use VideoImageUrl cover);
    user photos often return empty URL fields (anti-scrape) — callers should
    fall back to room browse images / introduction pictures.
    """
    if not album_payload:
        return []
    data = album_payload.get("data") or album_payload
    urls: list[str] = []
    seen: set[str] = set()

    def _ok(u: Any) -> bool:
        if not u or not isinstance(u, str):
            return False
        low = u.lower().strip()
        if not low.startswith("http"):
            return False
        if any(x in low for x in (".mp4", "video-preview", "/videos/")):
            # allow only if it looks like a cover jpg hosted under videos path
            if not any(low.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
                return False
        if "placeholder" in low or "np-pic.png" in low:
            return False
        return True

    def add(u: Any) -> None:
        if not _ok(u) or u in seen:
            return
        seen.add(u)
        urls.append(u)

    for cat in data.get("AlbumCategoryList") or []:
        if not isinstance(cat, dict):
            continue
        for pcat in cat.get("PictureCategoryList") or []:
            if not isinstance(pcat, dict):
                continue
            for pic in pcat.get("AlbumPictureList") or []:
                if not isinstance(pic, dict):
                    continue
                add(
                    pic.get("LargeUrl")
                    or pic.get("OtherUrl")
                    or pic.get("SmallUrl")
                    or pic.get("NewSmallUrl")
                    or pic.get("VideoImageUrl")
                )
                if len(urls) >= 60:
                    break
            if len(urls) >= 60:
                break
        if len(urls) >= 60:
            break
    return urls


def adapt_album_to_legacy(album_payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap getHotelAlbumPicture response into the legacy `hotelTopImage.imgUrlList`
    shape that `normalize.images_from_album` understands."""
    if not album_payload:
        return {}
    urls = images_from_album_picture(album_payload)
    return {
        "data": {
            "hotelTopImage": {
                "total": len(urls),
                "imgUrlList": [{"imgUrl": u} for u in urls],
            }
        }
    }


def build_fetch_result(
    *,
    hotel_id: int | str,
    hotel_meta: dict[str, Any],
    room_payload: dict[str, Any] | None,
    album_payload: dict[str, Any] | None,
    additional_payload: dict[str, Any] | None,
    check_in: str,
    check_out: str,
) -> dict[str, Any]:
    """Assemble an API-mode fetch result compatible with `build_hotel_document`.

    - `api`: the raw getHotelRoomListInland response (source = "api")
    - `album`: adapted to legacy shape
    - `additional`: raw getDetailAdditionalInfo response
    - `page_hotel`: minimal static info from the list meta (name/star/score/address)
    """
    data = (room_payload or {}).get("data") or {}
    source = "api" if data.get("physicRoomMap") else "none"
    page_hotel = {
        "hotel_id": hotel_id,
        "name": hotel_meta.get("name"),
        "star": hotel_meta.get("star"),
        "address": hotel_meta.get("address"),
        "score": hotel_meta.get("score"),
        "review_count": hotel_meta.get("comment_count"),
        "images": [],
    }
    return {
        "source": source,
        "hotel_id": hotel_id,
        "page_hotel": page_hotel,
        "dom_rooms": [],
        "room_detail_extras": [],
        "api": room_payload,
        "album": adapt_album_to_legacy(album_payload),
        "additional": additional_payload,
        "check_in": check_in,
        "check_out": check_out,
    }

# ===== inlined from ctrip_hotel/intl_client.py =====
# ---------------------------------------------------------------------------
# HKD -> CNY exchange rate
# ---------------------------------------------------------------------------

_EXCHANGE_SOURCES = [
    "https://open.er-api.com/v6/latest/HKD",
    "https://api.exchangerate-api.com/v4/latest/HKD",
]

EXCHANGE_FALLBACK = 0.9  # HKD -> CNY rough rate used if live APIs fail

_HOTEL_ID_RE = re.compile(r'("hotelId"\s*:\s*)\d+')


def fetch_hkd_cny_rate(timeout: int = 10) -> float:
    """Return the HKD->CNY exchange rate (e.g. 0.91)."""
    for url in _EXCHANGE_SOURCES:
        try:
            r = cffi_requests.get(url, timeout=timeout, impersonate="chrome")
            j = r.json()
            rates = j.get("rates") or {}
            cny = rates.get("CNY")
            if cny:
                return float(cny)
        except Exception:
            continue
    return EXCHANGE_FALLBACK


# ---------------------------------------------------------------------------
# International room-list endpoint
# ---------------------------------------------------------------------------

_INTL_ROOM_URL = "https://hk.trip.com/restapi/soa2/33269/getHotelRoomListOversea"


def _payload_kind(payload: dict[str, Any] | None) -> str:
    """Classify a room-list response: ok | token | session | error | empty."""
    if not payload:
        return "error"
    err = str(payload.get("error") or "")
    low = err.lower()
    if "430" in low or "whaleguard" in low:
        return "session"
    data = payload.get("data")
    if not isinstance(data, dict):
        if err:
            return "error"
        return "empty"
    code = data.get("htlSpiderActionErrorCode")
    if code in (4030, "4030", 430, "430"):
        return "token" if str(code) == "4030" else "session"
    if data.get("saleRoomMap"):
        return "ok"
    if err:
        return "error"
    return "empty"


class IntlRoomClient:
    """Browser page kept as a signer; room prices fetched via pure HTTP.

    Fast+stable pipeline:
      - Sign in small chunks (= intl_workers), HTTP that chunk immediately
        (tokens stay fresh).
      - On 4030: resign same body and retry.
      - On 430 / dead signature: re-warmup session, then retry.
    """

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        worker_id: int | None = None,
        seed_hotel_id: int | str | None = None,
    ) -> None:
        self.cfg = cfg
        self.worker_id = worker_id
        self.seed_hotel_id = seed_hotel_id
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._template_url: str = _INTL_ROOM_URL
        self._template_post: str = ""
        self._template_headers: dict[str, str] = {}
        self._lock = threading.RLock()
        self._session_fails = 0
        self._route_installed = False
        self._last_rewarm_at = 0.0
        self._rewarm_cooldown_sec = 15.0
        self._ready: bool = False
        self._ready_detail: str = ""
        self._session_channel = "intl_oversea"

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "IntlRoomClient":
        from playwright.sync_api import sync_playwright

        # Fail-fast: dead local proxy wastes minutes in headed Edge warmup loops.
        # （正常路径：crawl 启动时已弹窗处理；这里兜底二次调用/脚本直调）
        ok, msg = intl_proxy_ready(self.cfg, timeout=1.5)
        if not ok:
            raise RuntimeError(
                f"intl proxy not ready: {msg}. "
                "请先启动境外代理，或关闭 intl_price / 改正 intl_proxy。"
            )
        proxy = resolve_intl_proxy(self.cfg)
        if proxy:
            print(f"  [intl] proxy preflight ok: {msg}", flush=True)

        t0 = time.time()
        self._pw = sync_playwright().start()
        headed = bool(self.cfg.get("intl_headed", True))
        launch_kwargs: dict[str, Any] = {
            "headless": not headed,
            "args": [
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-background-networking",
            ],
        }
        # Prefer dedicated intl channel; fall back to shared browser_channel.
        channel = (
            self.cfg.get("intl_browser_channel") or self.cfg.get("browser_channel") or ""
        ).lower()
        try:
            if channel in {"msedge", "chrome"}:
                self._browser = self._pw.chromium.launch(
                    channel=channel, **launch_kwargs
                )
            else:
                self._browser = self._pw.chromium.launch(**launch_kwargs)
        except Exception:
            self._browser = self._pw.chromium.launch(**launch_kwargs)

        self._new_context_and_page()
        self._warmup()
        self._ensure_ready()
        print(f"  [intl] client ready {time.time() - t0:.1f}s", flush=True)
        return self

    def is_ready(self) -> bool:
        return bool(self._ready and self._template_post)

    def _ensure_ready(self) -> None:
        """Hard gate: diagnose → auto-fix → only then allow batch."""
        if not self._template_post:
            raise final_probe_error(
                channel="intl",
                reason="template_missing",
                detail="oversea template missing",
                hotel_id=self.seed_hotel_id,
                tried=[],
            )

        seeds = self._seed_ids()
        seed_idx = 0
        probe_id = seeds[seed_idx]
        attempts = max(int(self.cfg.get("warmup_probe_retries") or 4), 1)
        last_reason, last_detail = "empty", ""
        tried: list[str] = []

        for i in range(attempts):
            payload = self.fetch_room(probe_id)
            kind = _payload_kind(payload)
            reason, detail = classify_intl_probe(payload, kind=kind)
            if reason == "ok":
                self._ready = True
                self._ready_detail = detail
                print(
                    f"  [intl] probe ok ({i + 1}/{attempts}): hotel={probe_id} {detail}",
                    flush=True,
                )
                if self._context is not None:
                    save_storage_state(
                        self._session_channel,
                        self._context,
                        extra={"probe_hotel": probe_id, "detail": detail},
                    )
                return

            _, action, hint = INTL_PLAYBOOK.get(
                reason, (reason, "backoff", "查看日志")
            )
            if action == "abort":
                print_probe_diagnosis(
                    channel="intl",
                    reason=reason,
                    detail=detail,
                    action="不可自动修复，立即中止",
                    hint=hint,
                    attempt=i + 1,
                    attempts=attempts,
                    hotel_id=probe_id,
                )
                self._ready = False
                raise final_probe_error(
                    channel="intl",
                    reason=reason,
                    detail=detail,
                    hotel_id=probe_id,
                    tried=tried + [f"{reason}:abort"],
                )

            fix_desc = {
                "rewarm": "刷新签名浏览器会话",
                "resign_or_rewarm": "重签一次，仍失败则 rewarm",
                "next_seed": f"换探针酒店 → {seeds[min(seed_idx + 1, len(seeds) - 1)]}",
                "backoff": "短暂退避后重试",
            }.get(action, action)
            print_probe_diagnosis(
                channel="intl",
                reason=reason,
                detail=detail,
                action=f"执行 {action}: {fix_desc}",
                hint=hint,
                attempt=i + 1,
                attempts=attempts,
                hotel_id=probe_id,
            )
            last_reason, last_detail = reason, detail

            if action == "next_seed" and seed_idx + 1 < len(seeds):
                seed_idx += 1
                probe_id = seeds[seed_idx]
                self.seed_hotel_id = probe_id
                tried.append(f"{reason}:next_seed:{probe_id}")
                continue
            if action in {"rewarm", "resign_or_rewarm", "session"} or reason in {
                "session",
                "signature",
                "empty",
            }:
                # token(4030): fetch_room already resigned; escalate to rewarm.
                need_rewarm = action in {"rewarm", "resign_or_rewarm"} or reason in {
                    "session",
                    "signature",
                    "token",
                    "empty",
                }
                if need_rewarm:
                    tried.append(f"{reason}:rewarm")
                    try:
                        if seed_idx + 1 < len(seeds):
                            seed_idx += 1
                            self.seed_hotel_id = seeds[seed_idx]
                            probe_id = seeds[seed_idx]
                        self._rewarm(force=True)
                    except Exception as e:
                        print(f"  [intl] auto-fix rewarm failed: {e}", flush=True)
                        last_detail = f"{detail}; rewarm={e}"
                    continue
            tried.append(f"{reason}:backoff")
            time.sleep(0.45 * (i + 1))

        self._ready = False
        raise final_probe_error(
            channel="intl",
            reason=last_reason,
            detail=last_detail,
            hotel_id=probe_id,
            tried=tried,
        )

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    def _new_context_and_page(self) -> None:
        assert self._browser is not None
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        context_kwargs: dict[str, Any] = {
            "locale": "zh-HK",
            "timezone_id": "Asia/Hong_Kong",
            "extra_http_headers": {"Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8"},
        }
        proxy = self._proxy_url()
        if proxy:
            context_kwargs["proxy"] = {"server": str(proxy)}
        max_age = int(self.cfg.get("session_max_age_sec") or 6 * 3600)
        state_path = load_storage_state(self._session_channel, max_age_sec=max_age)
        if state_path is not None:
            context_kwargs["storage_state"] = str(state_path)
        self._context = self._browser.new_context(**context_kwargs)
        self._page = self._context.new_page()
        self._page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = window.chrome || { runtime: {} };
            """
        )
        self._route_installed = False

    def _proxy_url(self) -> str | None:
        proxy = self.cfg.get("intl_proxy") or self.cfg.get("proxy")
        if not proxy:
            return None
        s = str(proxy).strip()
        if not s:
            return None
        return s if "://" in s else f"http://{s}"

    def _proxies(self) -> dict[str, str] | None:
        proxy = self._proxy_url()
        if not proxy:
            return None
        return {"http": proxy, "https": proxy}

    def _retries(self) -> int:
        return max(int(self.cfg.get("intl_http_retries") or 2), 0)

    def _sign_batch_size(self, max_workers: int) -> int:
        raw = self.cfg.get("intl_sign_batch")
        if raw is None or raw == "":
            return max(int(max_workers), 1)
        return max(int(raw), 1)

    def _rewarm_threshold(self) -> int:
        return max(int(self.cfg.get("intl_rewarm_after") or 2), 1)

    # -- warmup / rewarm ---------------------------------------------------

    def _seed_ids(self) -> list[int | str]:
        seed = self.seed_hotel_id
        if seed is None:
            seed = int(self.cfg.get("seed_hotel_id") or 0) or 1
        seed_ids: list[int | str] = [seed]
        extras = self.cfg.get("seed_hotel_ids") or []
        seed_ids.extend(int(x) for x in extras if str(x).isdigit())
        return seed_ids

    def _warmup(self) -> None:
        """Capture an unused Oversea request template; keep page for signing."""
        assert self._page is not None
        t_warm = time.time()
        seed_ids = self._seed_ids()
        captured: dict[str, Any] = {}

        def handle_route(route) -> None:
            req = route.request
            url = req.url or ""
            # Abort heavy assets — signer only needs JS + the oversea XHR template.
            rtype = (req.resource_type or "").lower()
            if rtype in {"image", "media", "font", "stylesheet", "texttrack"}:
                try:
                    route.abort()
                except Exception:
                    route.continue_()
                return
            if "getHotelRoomListOversea" in url and "url" not in captured:
                captured["url"] = url
                captured["post"] = req.post_data or ""
                captured["headers"] = dict(req.headers)
                route.abort()
                return
            route.continue_()

        if self._route_installed:
            try:
                self._page.unroute("**/*")
            except Exception:
                pass
        # Catch-all so we can abort images while still capturing the room XHR.
        self._page.route("**/*", handle_route)
        self._route_installed = True

        proxy_dead = False
        for i, sid in enumerate(seed_ids):
            url = (
                f"https://hk.trip.com/hotels/detail/"
                f"?cityId={self.cfg.get('city_id', 1)}"
                f"&hotelId={sid}"
                f"&checkIn={self.cfg['check_in']}&checkOut={self.cfg['check_out']}"
            )
            print(
                f"  [intl] warmup attempt {i + 1}/{len(seed_ids)}: seed={sid}",
                flush=True,
            )
            captured.clear()
            goto_err = ""
            try:
                self._page.goto(url, timeout=45000, wait_until="domcontentloaded")
            except Exception as e:
                goto_err = str(e)
                print(f"  [warmup] goto warning: {e}", flush=True)
                # Dead proxy / tunnel errors will never recover by retrying seeds.
                low = goto_err.lower()
                if any(
                    x in low
                    for x in (
                        "err_proxy_connection_failed",
                        "err_tunnel_connection_failed",
                        "err_proxy_connection_timed_out",
                        "proxy connection",
                    )
                ):
                    proxy_dead = True
                    break
            if proxy_dead:
                break
            # Shorter wait if navigation already failed (no XHR expected).
            wait_sec = 5 if goto_err else 14
            deadline = time.time() + wait_sec
            while time.time() < deadline:
                if captured.get("post"):
                    break
                # signature may appear before XHR; poll both
                try:
                    if self._page.evaluate(
                        "() => typeof window.signature === 'function'"
                    ):
                        if captured.get("post"):
                            break
                except Exception:
                    pass
                self._page.wait_for_timeout(150)
                if time.time() < deadline:
                    try:
                        self._page.mouse.wheel(0, 1800)
                    except Exception:
                        pass
            if captured.get("post"):
                break

        if not captured.get("post"):
            if proxy_dead:
                raise RuntimeError(
                    "intl warmup failed: 代理连接失败（ERR_PROXY_*）。"
                    "请确认 intl_proxy 指向可用的境外代理，或暂时关闭 intl_price。"
                )
            raise RuntimeError(
                "intl warmup failed: 未捕获到 getHotelRoomListOversea 请求模板。"
                "常见原因：当前网络/IP 被国际站风控（whaleguard）。"
                "请配置境外代理（proxy / intl_proxy），优先 intl_headed=true，"
                "或换 seed_hotel_id。"
            )

        self._template_url = captured["url"]
        if self._template_url.startswith("//"):
            self._template_url = "https:" + self._template_url
        self._template_post = captured["post"]
        self._template_headers = captured["headers"] or {}

        ok = False
        for _ in range(16):
            ok = bool(
                self._page.evaluate("() => typeof window.signature === 'function'")
            )
            if ok:
                break
            self._page.wait_for_timeout(120)
        if not ok:
            raise RuntimeError(
                "intl warmup failed: 页面未暴露 window.signature，无法签发 phantom-token。"
            )
        self._session_fails = 0
        print(
            f"  [intl] warmup ok: oversea template captured "
            f"(signer ready) {time.time() - t_warm:.1f}s",
            flush=True,
        )

    def _rewarm(self, *, force: bool = False) -> bool:
        """Refresh signer session. Rate-limited to avoid WhaleGuard storms."""
        with self._lock:
            now = time.time()
            if (
                not force
                and (now - self._last_rewarm_at) < self._rewarm_cooldown_sec
            ):
                return False
            print("  [intl] rewarm: refreshing signer session…", flush=True)
            self._new_context_and_page()
            self._warmup()
            self._last_rewarm_at = time.time()
            return True

    def _maybe_rewarm(self, kind: str) -> None:
        if kind == "ok":
            self._session_fails = 0
            return
        if kind != "session":
            return
        self._session_fails += 1
        if self._session_fails >= self._rewarm_threshold():
            try:
                if self._rewarm():
                    self._session_fails = 0
            except Exception as e:
                print(f"  [intl] rewarm failed: {e}", flush=True)

    # -- signing / headers -------------------------------------------------

    def _body_for_hotel(self, hotel_id: int | str) -> str:
        """Rewrite search.hotelId in the exact captured POST string."""
        body, n = _HOTEL_ID_RE.subn(
            lambda m: m.group(1) + str(int(hotel_id)),
            self._template_post,
            count=1,
        )
        if n != 1:
            try:
                obj = json.loads(self._template_post)
                obj.setdefault("search", {})["hotelId"] = int(hotel_id)
                return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
            except Exception:
                return self._template_post
        return body

    def _sign(self, body_str: str) -> str:
        assert self._page is not None
        with self._lock:
            try:
                token = self._page.evaluate("(s) => window.signature(s)", body_str)
            except Exception as e:
                raise RuntimeError(f"signature() call failed: {e}") from e
        if not isinstance(token, str) or not token.startswith("100"):
            raise RuntimeError(f"signature() returned invalid token: {token!r}")
        return token

    def _sign_many(self, bodies: list[str]) -> list[str]:
        """Sign many bodies in one page.evaluate (far fewer Playwright round-trips)."""
        if not bodies:
            return []
        if len(bodies) == 1:
            return [self._sign(bodies[0])]
        assert self._page is not None
        with self._lock:
            try:
                tokens = self._page.evaluate(
                    """(arr) => {
                        if (typeof window.signature !== 'function') {
                            throw new Error('signature missing');
                        }
                        return arr.map((s) => window.signature(s));
                    }""",
                    bodies,
                )
            except Exception as e:
                raise RuntimeError(f"signature() batch failed: {e}") from e
        if not isinstance(tokens, list) or len(tokens) != len(bodies):
            raise RuntimeError(f"signature() batch size mismatch: {tokens!r}")
        out: list[str] = []
        for token in tokens:
            if not isinstance(token, str) or not token.startswith("100"):
                raise RuntimeError(f"signature() returned invalid token: {token!r}")
            out.append(token)
        return out

    def _sign_safe(self, body_str: str) -> str:
        """Sign; on failure rewarm once (rate-limited) and retry."""
        try:
            return self._sign(body_str)
        except Exception:
            # Respect cooldown — force=True caused rewarm storms under 430.
            self._rewarm(force=False)
            return self._sign(body_str)

    def _sign_many_safe(self, bodies: list[str]) -> list[str]:
        try:
            return self._sign_many(bodies)
        except Exception:
            self._rewarm(force=False)
            return self._sign_many(bodies)

    def _cookies(self) -> str:
        assert self._context is not None
        try:
            return "; ".join(
                f"{c['name']}={c['value']}" for c in self._context.cookies()
            )
        except Exception:
            return ""

    def _base_headers(self) -> dict[str, str]:
        skip = {
            "content-length",
            "host",
            "connection",
            "accept-encoding",
            "phantom-token",
            "cookie",
        }
        base: dict[str, str] = {}
        for k, v in (self._template_headers or {}).items():
            if k.lower() in skip or v is None:
                continue
            base[k] = v
        if not any(k.lower() == "origin" for k in base):
            base["origin"] = "https://hk.trip.com"
        if not any(k.lower() == "content-type" for k in base):
            base["content-type"] = "application/json"
        return base

    def _headers(self, token: str, cookie: str | None = None) -> dict[str, str]:
        base = self._base_headers()
        base["cookie"] = cookie if cookie is not None else self._cookies()
        base["phantom-token"] = token
        return base

    # -- price fetch (pure HTTP) -------------------------------------------

    def _http_post(self, body_str: str, token: str, cookie: str | None = None) -> dict[str, Any]:
        try:
            r = session_post(
                self._template_url,
                data=body_str.encode("utf-8"),
                headers=self._headers(token, cookie=cookie),
                proxies=self._proxies(),
                impersonate="chrome",
                timeout=30,
                tag="intl_room",
            )
            text = r.text or ""
            if not text.startswith("{"):
                return {
                    "error": f"http {r.status_code}: {text[:120]}",
                    "data": {},
                }
            j = json.loads(text)
        except Exception as e:
            return {"error": str(e), "data": {}}
        if not isinstance(j, dict):
            return {"error": "non-json response", "data": {}}
        return j

    def fetch_room(self, hotel_id: int | str) -> dict[str, Any]:
        """Sign + HTTP with resign/rewarm retries."""
        if not self._template_post:
            return {"error": "client not warmed up", "data": {}}
        body_str = self._body_for_hotel(hotel_id)
        last: dict[str, Any] = {"error": "no attempt", "data": {}}
        for attempt in range(self._retries() + 1):
            try:
                token = self._sign_safe(body_str)
            except Exception as e:
                last = {"error": str(e), "data": {}}
                continue
            last = self._http_post(body_str, token)
            kind = _payload_kind(last)
            if kind == "ok":
                self._session_fails = 0
                return last
            self._maybe_rewarm(kind)
            if attempt < self._retries():
                time.sleep(0.15 * (attempt + 1))
        return last

    def _sign_http_wave(
        self,
        hotel_ids: list[int | str],
        *,
        workers: int,
    ) -> dict[int | str, dict[str, Any]]:
        """Sign a small wave (batched) then HTTP-fetch in parallel (fresh cookies)."""

        bodies: list[tuple[int | str, str]] = []
        out: dict[int | str, dict[str, Any]] = {}
        for hid in hotel_ids:
            bodies.append((hid, self._body_for_hotel(hid)))

        try:
            tokens = self._sign_many_safe([b for _, b in bodies])
        except Exception as e:
            for hid, _ in bodies:
                out[hid] = {"error": str(e), "data": {}}
            return out

        signed: list[tuple[int | str, str, str]] = []
        for (hid, body_str), token in zip(bodies, tokens):
            signed.append((hid, body_str, token))

        if not signed:
            return out

        cookie = self._cookies()

        def _one(job: tuple[int | str, str, str]) -> tuple[int | str, dict[str, Any]]:
            hid, body_str, token = job
            return hid, self._http_post(body_str, token, cookie=cookie)

        with ThreadPoolExecutor(max_workers=min(workers, len(signed))) as ex:
            futs = [ex.submit(_one, job) for job in signed]
            for fut in as_completed(futs):
                hid, payload = fut.result()
                out[hid] = payload
        return out

    def fetch_room_batch(
        self,
        hotel_ids: list[int | str],
        *,
        max_workers: int = 8,
    ) -> list[dict[str, Any]]:
        """Chunked sign→HTTP: short-lived tokens, limited rewarm, one retry wave."""
        if not hotel_ids:
            return []
        if not self.is_ready():
            raise RuntimeError(
                "拒绝国际价批量抓取：预热探针未通过 "
                f"(ready={self._ready}, detail={self._ready_detail!r})"
            )
        if not self._template_post:
            return [{"error": "client not warmed up", "data": {}} for _ in hotel_ids]

        workers = max(int(max_workers), 1)
        chunk_size = self._sign_batch_size(workers)
        results: dict[int | str, dict[str, Any]] = {}
        n_chunks = (len(hotel_ids) + chunk_size - 1) // chunk_size

        for i in range(0, len(hotel_ids), chunk_size):
            chunk_idx = i // chunk_size
            chunk = list(hotel_ids[i : i + chunk_size])
            wave = self._sign_http_wave(chunk, workers=workers)

            retry_ids: list[int | str] = []
            session_hits = 0
            ok_hits = 0
            for hid in chunk:
                payload = wave.get(hid) or {"error": "missing", "data": {}}
                kind = _payload_kind(payload)
                if kind == "ok":
                    results[hid] = payload
                    self._session_fails = 0
                    ok_hits += 1
                else:
                    if kind == "session":
                        session_hits += 1
                    retry_ids.append(hid)

            if retry_ids and self._retries() > 0:
                # Only rewarm when the whole wave was session-blocked.
                if session_hits and session_hits >= len(chunk):
                    try:
                        self._rewarm()
                    except Exception as e:
                        print(f"  [intl] rewarm failed: {e}", flush=True)
                    time.sleep(0.8)
                else:
                    time.sleep(0.15)
                wave2 = self._sign_http_wave(retry_ids, workers=workers)
                for hid in retry_ids:
                    payload = wave2.get(hid) or wave.get(hid) or {
                        "error": "retry missing",
                        "data": {},
                    }
                    results[hid] = payload
                    if _payload_kind(payload) == "ok":
                        self._session_fails = 0
                        ok_hits += 1
            else:
                for hid in retry_ids:
                    results[hid] = wave.get(hid) or {"error": "failed", "data": {}}

            # Pace chunks only when needed — skip after last; shorten on healthy waves.
            if chunk_idx + 1 < n_chunks:
                if session_hits >= max(len(chunk) // 2, 1):
                    time.sleep(0.8)
                elif ok_hits == len(chunk):
                    time.sleep(0.12)
                else:
                    time.sleep(0.35)

        return [
            results.get(hid) or {"error": "missing result", "data": {}}
            for hid in hotel_ids
        ]


# ---------------------------------------------------------------------------
# Parsing / merging
# ---------------------------------------------------------------------------


def normalize_intl_prices(
    payload: dict[str, Any] | None,
    *,
    check_in: str,
    check_out: str,
    rate: float,
) -> dict[str, Any]:
    """Parse getHotelRoomListOversea into physical-room → plan prices."""
    out: dict[str, Any] = {
        "currency": "HKD",
        "exchange_rate": rate,
        "exchange_currency": "CNY",
        "rooms": [],
    }
    if not payload:
        return out
    data = payload.get("data") or {}
    if data.get("htlSpiderActionErrorCode"):
        return out
    physic = data.get("physicRoomMap") or {}
    sale = data.get("saleRoomMap") or {}
    if not isinstance(sale, dict):
        return out

    by_physic: dict[str, list[dict[str, Any]]] = {}
    for s in sale.values():
        if not isinstance(s, dict):
            continue
        phys_id = s.get("physicalRoomId")
        by_physic.setdefault(str(phys_id), []).append(s)

    for pid, sales in by_physic.items():
        p_name = None
        if isinstance(physic, dict) and pid in physic:
            p_name = (physic[pid] or {}).get("name")
        plans = []
        for s in sales:
            price = _parse_price(s)
            plans.append(
                {
                    "plan_id": s.get("id"),
                    "room_name": s.get("name") or p_name,
                    "price_hkd": price,
                    "price_cny": round(price * rate, 2) if price is not None else None,
                    "summary": _summary_from_tags(s),
                    "meal": _meal_from_sale(s),
                    "cancel": _cancel_from_sale(s),
                    "confirm": _confirm_from_tags(s),
                    "occupancy": _occupancy_from_sale(s),
                    "left": _left_from_tags(s),
                    "folded": bool(s.get("isFoldStatus")),
                    "display_price": _display_price(s),
                }
            )
        prices = [p["price_hkd"] for p in plans if p["price_hkd"] is not None]
        start_hkd = min(prices) if prices else None
        out["rooms"].append(
            {
                "physical_room_id": pid,
                "room_name": p_name or (plans[0]["room_name"] if plans else None),
                "start_price_hkd": start_hkd,
                "start_price_cny": (
                    round(start_hkd * rate, 2) if start_hkd is not None else None
                ),
                "plans": plans,
            }
        )
    return out


def _parse_price(s: dict[str, Any]) -> float | None:
    """Extract numeric HKD price from a saleRoom (prefers priceInfo)."""
    pi = s.get("priceInfo")
    if isinstance(pi, dict):
        disp = str(pi.get("displayPrice") or "")
        if disp and ("?" in disp or disp.strip() in {"", "HK$", "¥"}):
            return None
        for k in ("price", "deletePricewithOutCurrency"):
            v = pi.get(k)
            if v is None:
                continue
            try:
                n = float(v)
            except (TypeError, ValueError):
                continue
            if n > 0:
                return n
        if disp:
            n = _digits_money(disp)
            if n is not None and n > 0:
                return n
    for k in ("priceStr", "showPrice", "showPriceStr", "price", "avgPrice"):
        v = s.get(k)
        if v is None:
            continue
        t = str(v).strip()
        if not t or t in {"?", "0"} or "?" in t:
            continue
        try:
            n = float(t)
            if n > 0:
                return n
        except ValueError:
            n = _digits_money(t)
            if n is not None and n > 0:
                return n
    return None


def _display_price(s: dict[str, Any]) -> str | None:
    pi = s.get("priceInfo")
    if isinstance(pi, dict) and pi.get("displayPrice"):
        return str(pi["displayPrice"])
    return None


def _digits_money(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _summary_from_tags(s: dict[str, Any]) -> str | None:
    tags = _all_tags(s)
    skip = ("人入住", "早餐", "取消", "确认", "仅剩", "开票", "含")
    summary_parts = [t for t in tags if not any(k in t for k in skip)]
    return "；".join(summary_parts) if summary_parts else None


def _all_tags(s: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for tl in (s.get("tagInfoList") or []) + (s.get("serviceTagList") or []):
        if isinstance(tl, dict) and tl.get("tagTitle"):
            tags.append(str(tl["tagTitle"]))
    return tags


def _meal_from_sale(s: dict[str, Any]) -> str | None:
    meal = s.get("mealInfo")
    if isinstance(meal, dict):
        for k in ("title", "desc", "mealType"):
            if meal.get(k):
                return str(meal[k])
    for t in _all_tags(s):
        if "早餐" in t or "含早" in t or "无早" in t:
            return t
    return None


def _cancel_from_sale(s: dict[str, Any]) -> str | None:
    cancel = s.get("cancelInfo")
    if isinstance(cancel, dict):
        for k in ("title", "desc", "cancelType"):
            if cancel.get(k):
                return str(cancel[k])
    for t in _all_tags(s):
        if "取消" in t or "不可退" in t:
            return t
    return None


def _confirm_from_tags(s: dict[str, Any]) -> str | None:
    for t in _all_tags(s):
        if "确认" in t or "立即" in t:
            return t
    return None


def _occupancy_from_sale(s: dict[str, Any]) -> int | None:
    g = s.get("guestCountInfo")
    if isinstance(g, dict):
        for k in ("adult", "guestCount", "maxGuest"):
            if g.get(k) is not None:
                try:
                    return int(g[k])
                except (TypeError, ValueError):
                    pass
    for t in _all_tags(s):
        if "人入住" in t or "成人" in t:
            m = re.search(r"(\d+)\s*人", t)
            if m:
                return int(m.group(1))
    return None


def _left_from_tags(s: dict[str, Any]) -> int | None:
    for t in _all_tags(s):
        if "仅剩" in t:
            m = re.search(r"仅剩\s*(\d+)\s*间", t)
            if m:
                return int(m.group(1))
    return None


def merge_prices_into_doc(
    doc: dict[str, Any],
    price_info: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach international price info to a hotel document."""
    doc = dict(doc)
    hotel = dict(doc.get("hotel") or {})
    if price_info:
        hotel["price_info"] = price_info
        hotel["min_price_hkd"] = _min_overall(price_info)
        if hotel.get("min_price_hkd") is not None:
            hotel["min_price_cny"] = round(
                hotel["min_price_hkd"] * price_info.get("exchange_rate", 0.9), 2
            )
    doc["hotel"] = hotel

    plans_by_physic: dict[str, list[dict[str, Any]]] = {}
    if price_info:
        for r in price_info.get("rooms") or []:
            plans_by_physic[str(r.get("physical_room_id"))] = r.get("plans") or []

    rooms = []
    for room in doc.get("rooms") or []:
        room = dict(room)
        rid = str(room.get("room_id"))
        if rid in plans_by_physic:
            room["prices"] = plans_by_physic[rid]
        rooms.append(room)
    doc["rooms"] = rooms
    return doc


def _min_overall(price_info: dict[str, Any]) -> float | None:
    vals = [
        r.get("start_price_hkd")
        for r in price_info.get("rooms") or []
        if r.get("start_price_hkd") is not None
    ]
    return min(vals) if vals else None

# ---------------------------------------------------------------------------
# netutil (slim, no popup)
# ---------------------------------------------------------------------------

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
    """TCP connect check. Returns (ok, message). Empty proxy -> (True, 'direct')."""
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
        return False, "\u672a\u914d\u7f6e intl_proxy / proxy\uff08\u56fd\u9645\u7ad9\u9700\u8981\u5883\u5916\u51fa\u53e3\uff09"
    ok, msg = proxy_reachable(proxy, timeout=timeout)
    if not ok:
        return False, f"\u4ee3\u7406\u4e0d\u53ef\u8fbe\uff1a{msg}"
    return True, msg


# ---------------------------------------------------------------------------
# raw payload helper
# ---------------------------------------------------------------------------

def save_raw_payloads(run_dir: Path, name: str, payloads: list[dict[str, Any]]) -> None:
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    for i, p in enumerate(payloads):
        (raw_dir / f"{name}_{i + 1}.json").write_text(
            json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# crawl engine (API path only)
# ---------------------------------------------------------------------------

def fetch_hotel_catalog(cfg: dict[str, Any], run_dir: Path) -> list[dict[str, Any]]:
    proxies = extract_proxy_pool(cfg)
    max_hotels = cfg.get("max_hotels")
    max_items = int(max_hotels) + 5 if max_hotels else None
    items = fetch_hotel_list_pure(
        city_id=cfg["city_id"],
        check_in=cfg["check_in"],
        check_out=cfg["check_out"],
        pages=int(cfg.get("pages") or 1),
        page_size=int(cfg.get("page_size") or 20),
        delay_ms=int(cfg.get("delay_ms") or 0),
        proxies=proxies,
        max_items=max_items,
    )
    hotels = normalize_list_payloads(items)
    if hotels:
        return dedupe_hotels(hotels)
    return []


def prepare_hotel_queue(
    hotels: list[dict[str, Any]], cfg: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    hotels = dedupe_hotels(hotels)
    skipped: list[dict[str, Any]] = []
    if cfg.get("skip_done", True):
        done = load_done(cfg["output_dir"])
        hotels, skipped = filter_new_hotels(
            hotels,
            done,
            city_id=cfg["city_id"],
            check_in=cfg["check_in"],
            check_out=cfg["check_out"],
        )
        if skipped:
            print(f"\u9632\u91cd\u590d: \u8df3\u8fc7\u5df2\u6293 {len(skipped)} \u5bb6")
    max_hotels = cfg.get("max_hotels")
    if max_hotels:
        hotels = hotels[: int(max_hotels)]
    return hotels, skipped


def _fetch_intl_payloads(
    hotels: list[dict[str, Any]],
    cfg: dict[str, Any],
    run_dir: Path,
) -> tuple[list[dict[str, Any]], float]:
    """Warm intl signer + fetch all oversea payloads. Safe to run beside domestic."""
    t0 = time.time()
    worker_dir = run_dir / "workers" / "intl"
    worker_dir.mkdir(parents=True, exist_ok=True)
    intl_ids = [h["hotel_id"] for h in hotels]
    seed = intl_ids[0] if intl_ids else cfg.get("seed_hotel_id")

    rate_box: dict[str, float] = {}

    def _rate() -> None:
        rate_box["rate"] = fetch_hkd_cny_rate()

    rate_thread = threading.Thread(target=_rate, name="fx-rate", daemon=True)
    rate_thread.start()

    with IntlRoomClient(cfg, worker_id=0, seed_hotel_id=seed) as intl:
        if not intl.is_ready():
            raise RuntimeError(
                f"intl warmup gate: client not ready ({intl._ready_detail!r})"
            )
        t_http = time.time()
        payloads = intl.fetch_room_batch(
            intl_ids, max_workers=max(int(cfg.get("intl_workers") or 4), 1)
        )
        print(f"[intl] batch HTTP {time.time() - t_http:.1f}s", flush=True)
    rate_thread.join(timeout=3)
    rate = float(rate_box.get("rate") or EXCHANGE_FALLBACK)
    for h, payload in zip(hotels, payloads):
        save_raw_payloads(worker_dir, f"intl_room_{h['hotel_id']}", [payload])
    print(
        f"[intl] HTTP \u5b8c\u6210 {len(hotels)} \u5bb6 \u8017\u65f6 {time.time() - t0:.1f}s (\u6c47\u7387 {rate:.4f})",
        flush=True,
    )
    return payloads, rate


def _apply_intl_payloads(
    docs: list[dict[str, Any]],
    hotels: list[dict[str, Any]],
    payloads: list[dict[str, Any]],
    rate: float,
    cfg: dict[str, Any],
    run_dir: Path,
) -> list[dict[str, Any]]:
    hotels_dir = run_dir / "hotels"
    doc_index = {d.get("hotel_id"): i for i, d in enumerate(docs)}
    merged = 0
    for h, payload in zip(hotels, payloads):
        hid = h["hotel_id"]
        if not payload or payload.get("error"):
            continue
        price_info = normalize_intl_prices(
            payload,
            check_in=cfg["check_in"],
            check_out=cfg["check_out"],
            rate=rate,
        )
        if not price_info.get("rooms"):
            continue
        idx = doc_index.get(hid)
        if idx is None:
            continue
        merged_doc = merge_prices_into_doc(docs[idx], price_info)
        docs[idx] = merged_doc
        write_json(hotels_dir / f"{hid}.json", merged_doc)
        merged += 1
    print(f"[intl] \u4ef7\u683c\u5408\u5e76 {merged}/{len(hotels)} \u5bb6", flush=True)
    return docs


def _merge_intl_prices(
    docs: list[dict[str, Any]],
    hotels: list[dict[str, Any]],
    cfg: dict[str, Any],
    run_dir: Path,
) -> list[dict[str, Any]]:
    if not cfg.get("intl_price") or not hotels:
        return docs
    t0 = time.time()
    try:
        payloads, rate = _fetch_intl_payloads(hotels, cfg, run_dir)
        docs = _apply_intl_payloads(docs, hotels, payloads, rate, cfg, run_dir)
        print(f"[intl] \u603b\u8017\u65f6 {time.time() - t0:.1f}s", flush=True)
    except Exception as e:
        print(f"[intl] \u4ef7\u683c\u6293\u53d6\u5931\u8d25: {e}", flush=True)
    return docs


def _worker_crawl_api(
    worker_id: int,
    hotels: list[dict[str, Any]],
    cfg: dict[str, Any],
    run_dir: str,
) -> dict[str, Any]:
    run = Path(run_dir)
    worker_dir = run / "workers" / f"w{worker_id}"
    hotels_dir = run / "hotels"
    hotels_dir.mkdir(parents=True, exist_ok=True)
    worker_dir.mkdir(parents=True, exist_ok=True)

    docs: list[dict[str, Any]] = []
    ok_ids: list[Any] = []
    err_ids: list[Any] = []
    max_workers = max(int(cfg.get("api_workers") or 8), 1)
    hotel_ids = [h["hotel_id"] for h in hotels]

    print(f"[w{worker_id}] API \u6a21\u5f0f\u5f00\u59cb\uff0c\u672c\u7ec4 {len(hotels)} \u5bb6 (\u5e76\u53d1 {max_workers})")
    t_warm = time.time()
    try:
        client_ctx = ApiRoomClient(cfg, worker_id=worker_id)
    except Exception as e:
        print(f"[w{worker_id}] \u9884\u70ed\u5ba2\u6237\u7aef\u521b\u5efa\u5931\u8d25\uff0c\u5df2\u4e2d\u6b62: {e}", flush=True)
        raise

    with client_ctx as client:
        if not client.is_ready():
            raise RuntimeError(
                f"API warmup gate: client not ready ({client._ready_detail!r})"
            )
        print(
            f"[w{worker_id}] warmup+probe {time.time() - t_warm:.1f}s "
            f"({client._ready_detail})",
            flush=True,
        )
        t_rooms = time.time()
        room_payloads = client.fetch_room_batch(hotel_ids, max_workers=max_workers)
        print(
            f"[w{worker_id}] rooms HTTP {len(hotels)} hotels {time.time() - t_rooms:.1f}s",
            flush=True,
        )
        t_enrich = time.time()
        enrich = client.fetch_enrich_batch(hotel_ids, max_workers=max_workers)
        print(
            f"[w{worker_id}] album/additional {len(hotels)} hotels {time.time() - t_enrich:.1f}s",
            flush=True,
        )
        for h, room_payload in zip(hotels, room_payloads):
            hid = h["hotel_id"]
            name = h.get("name") or ""
            album_payload, additional_payload = enrich.get(hid, ({}, {}))
            try:
                result = build_fetch_result(
                    hotel_id=hid,
                    hotel_meta=h,
                    room_payload=room_payload,
                    album_payload=album_payload,
                    additional_payload=additional_payload,
                    check_in=cfg["check_in"],
                    check_out=cfg["check_out"],
                )
                doc = build_hotel_document(
                    hotel_meta=h,
                    page_hotel=result.get("page_hotel"),
                    fetch_result=result,
                    check_in=cfg["check_in"],
                    check_out=cfg["check_out"],
                )
                gaps = document_gaps(doc)
                if gaps and "rooms" in gaps:
                    print(f"[w{worker_id}]   retry (no rooms) {hid}")
                    room_payload = client.fetch_room(hid)
                    result = build_fetch_result(
                        hotel_id=hid,
                        hotel_meta=h,
                        room_payload=room_payload,
                        album_payload=album_payload,
                        additional_payload=additional_payload,
                        check_in=cfg["check_in"],
                        check_out=cfg["check_out"],
                    )
                    doc = build_hotel_document(
                        hotel_meta=h,
                        page_hotel=result.get("page_hotel"),
                        fetch_result=result,
                        check_in=cfg["check_in"],
                        check_out=cfg["check_out"],
                    )
                    gaps = document_gaps(doc)

                save_raw_payloads(
                    worker_dir,
                    f"room_{hid}",
                    [
                        {"room": room_payload},
                        {"album": album_payload},
                        {"additional": additional_payload},
                    ],
                )
                write_json(hotels_dir / f"{hid}.json", doc)
                docs.append(doc)
                n_rooms = len(doc.get("rooms") or [])
                hotel = doc.get("hotel") or {}
                n_himgs = len(hotel.get("images") or [])
                if n_rooms == 0:
                    err_ids.append(hid)
                    print(f"[w{worker_id}]   ! {hid} no rooms gaps={gaps}")
                else:
                    ok_ids.append(hid)
                    print(
                        f"[w{worker_id}]   ok {hid} rooms={n_rooms} imgs={n_himgs} "
                        f"nearby={sum(len(hotel.get('nearby',{}).get(k) or []) for k in ('metro','airport','train','other'))} "
                        f"gaps={gaps or 'none'}"
                    )
                    mark_done(
                        cfg["output_dir"],
                        done_key(
                            city_id=cfg["city_id"],
                            hotel_id=hid,
                            check_in=cfg["check_in"],
                            check_out=cfg["check_out"],
                        ),
                        meta={
                            "hotel_id": hid,
                            "name": hotel.get("name") or name,
                            "worker": worker_id,
                            "rooms": n_rooms,
                            "gaps": gaps,
                        },
                    )
            except Exception as e:
                err_ids.append(hid)
                print(f"[w{worker_id}]   !! {hid} {e}")

    write_json(
        worker_dir / "summary.json",
        {"worker_id": worker_id, "ok": ok_ids, "err": err_ids, "docs": len(docs)},
    )
    print(f"[w{worker_id}] \u7ed3\u675f ok={len(ok_ids)} err={len(err_ids)}")
    return {
        "worker_id": worker_id,
        "docs": docs,
        "ok_ids": ok_ids,
        "err_ids": err_ids,
        "hotels": hotels,
    }


def crawl_rooms_parallel(
    hotels: list[dict[str, Any]],
    cfg: dict[str, Any],
    run_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    t_all = time.time()
    # API mode: single worker (browser only for one token warmup).
    workers = 1
    groups = split_groups(hotels, workers)
    jobs = [(i, g) for i, g in enumerate(groups) if g]
    write_json(
        run_dir / "groups.json",
        {
            "workers": workers,
            "groups": [
                {
                    "worker_id": i,
                    "count": len(g),
                    "hotel_ids": [h["hotel_id"] for h in g],
                }
                for i, g in jobs
            ],
        },
    )
    for i, g in jobs:
        print(f"\u5206\u7ec4 w{i}: {len(g)} \u5bb6 -> {[h['hotel_id'] for h in g]}")

    all_docs: list[dict[str, Any]] = []
    crawled_hotels: list[dict[str, Any]] = []

    worker_fn = _worker_crawl_api
    parallel_intl = bool(cfg.get("intl_price") and hotels)

    intl_payloads: list[dict[str, Any]] | None = None
    intl_rate = 0.0
    intl_error: Exception | None = None

    if parallel_intl:
        print("[crawl] \u56fd\u5185 + \u56fd\u9645\u4ef7\u5e76\u884c\u2026", flush=True)
        with ThreadPoolExecutor(max_workers=2) as ex:
            domestic_fut = ex.submit(worker_fn, jobs[0][0], jobs[0][1], cfg, str(run_dir))
            intl_fut = ex.submit(_fetch_intl_payloads, hotels, cfg, run_dir)
            res = domestic_fut.result()
            all_docs.extend(res["docs"])
            crawled_hotels.extend(res["hotels"])
            try:
                intl_payloads, intl_rate = intl_fut.result()
            except Exception as e:
                intl_error = e
        if intl_error:
            print(f"[intl] \u4ef7\u683c\u6293\u53d6\u5931\u8d25: {intl_error}", flush=True)
        elif intl_payloads is not None:
            all_docs = _apply_intl_payloads(
                all_docs, crawled_hotels, intl_payloads, intl_rate, cfg, run_dir
            )
    elif jobs:
        res = worker_fn(jobs[0][0], jobs[0][1], cfg, str(run_dir))
        all_docs.extend(res["docs"])
        crawled_hotels.extend(res["hotels"])
        if cfg.get("intl_price"):
            all_docs = _merge_intl_prices(all_docs, crawled_hotels, cfg, run_dir)

    catalog = []
    for d in all_docs:
        h = d.get("hotel") or {}
        catalog.append(
            {
                "hotel_id": d.get("hotel_id"),
                "name": h.get("name"),
                "star": h.get("star"),
                "score": h.get("score"),
                "address": h.get("address"),
                "cover": (h.get("images") or [None])[0],
                "room_count": len(d.get("rooms") or []),
                "min_price_hkd": h.get("min_price_hkd"),
                "min_price_cny": h.get("min_price_cny"),
                "file": f"hotels/{d.get('hotel_id')}.json",
            }
        )
    write_json(run_dir / "catalog.json", catalog)
    write_jsonl(run_dir / "catalog.jsonl", catalog)
    print(f"[crawl] \u603b\u8017\u65f6 {time.time() - t_all:.1f}s", flush=True)
    return crawled_hotels, all_docs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="\u643a\u7a0b\u9152\u5e97 API \u6293\u53d6\uff08\u5217\u8868+\u623f\u6001+\u56fd\u9645\u4ef7\uff09")
    p.add_argument("--config", default=None)
    p.add_argument("--city-id", type=int, default=None)
    p.add_argument("--max-hotels", type=int, default=None)
    p.add_argument("--no-skip-done", action="store_true")
    p.add_argument("--no-intl-price", action="store_true")
    p.add_argument("--headed", action=argparse.BooleanOptionalAction, default=None)
    args = p.parse_args(argv)

    cfg = load_config(Path(args.config) if args.config else None)
    if args.headed is not None:
        cfg["headed"] = args.headed
        cfg["api_headed"] = args.headed
    if args.city_id is not None:
        cfg["city_id"] = args.city_id
    if args.max_hotels is not None:
        cfg["max_hotels"] = args.max_hotels
    if args.no_skip_done:
        cfg["skip_done"] = False
    if args.no_intl_price:
        cfg["intl_price"] = False

    cfg["mode"] = "api"

    if cfg.get("intl_price"):
        ok, detail = intl_proxy_ready(cfg)
        if not ok:
            print(f"[intl] \u4ee3\u7406\u672a\u5c31\u7eea: {detail}", flush=True)
            cfg["intl_price"] = False
            print("[intl] \u5df2\u5173\u95ed\u56fd\u9645\u4ef7\uff0c\u4ec5\u6293\u56fd\u5185\u6570\u636e\u3002", flush=True)

    run_dir = new_run_dir(cfg["output_dir"])
    write_json(run_dir / "config.used.json", cfg)
    print(
        f"\u8fd0\u884c\u76ee\u5f55: {run_dir}"
        f"\n\u6a21\u5f0f={cfg.get('mode', 'api')} \u57ce\u5e02={cfg['city_id']} {cfg.get('city_name', '')} "
        f"{cfg['check_in']}~{cfg['check_out']} | skip_done={cfg.get('skip_done', True)} "
        f"intl_price={cfg.get('intl_price')}"
    )

    hotels = fetch_hotel_catalog(cfg, run_dir)
    todo, skipped = prepare_hotel_queue(hotels, cfg)
    write_json(
        run_dir / "queue.json",
        {
            "listed": len(hotels),
            "skipped_done": len(skipped),
            "todo": len(todo),
            "todo_ids": [h["hotel_id"] for h in todo],
        },
    )
    print(f"\u5217\u8868 {len(hotels)} \u2192 \u5f85\u6293 {len(todo)}\uff08\u8df3\u8fc7 {len(skipped)}\uff09")
    if not todo:
        print("\u6ca1\u6709\u5f85\u6293\u9152\u5e97\u3002\u52a0 --no-skip-done \u53ef\u5f3a\u5236\u91cd\u6293\u3002")
        write_json(run_dir / "catalog.json", [])
        return 0

    _, docs = crawl_rooms_parallel(todo, cfg, run_dir)
    write_json(Path(cfg["output_dir"]) / "latest.json", {"run_dir": str(run_dir)})
    rooms = sum(len(d.get("rooms") or []) for d in docs)
    imgs = sum(
        len((d.get("hotel") or {}).get("images") or [])
        + sum(len(r.get("images") or []) for r in (d.get("rooms") or []))
        for d in docs
    )
    print(f"\u5b8c\u6210: {len(docs)} \u9152\u5e97 / {rooms} \u623f\u578b / \u56fe\u7247URL {imgs} \u6761")
    print(f"\u7ed3\u6784\u5316\u6570\u636e: {run_dir / 'hotels'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
