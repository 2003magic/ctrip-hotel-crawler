from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = ROOT / "config.example.yaml"

DEFAULTS: dict[str, Any] = {
    "city_id": 1,
    "city_name": "北京",
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
    "workers": 1,  # parallel browsers; each uses .browser-profile/wN
    "workers_headed": False,  # detail workers headless by default when multi
    "skip_done": True,  # skip hotels already in data/state/done.jsonl
    "group_by": "round_robin",
    # API mode: mode: "api" uses pure-HTTP list + single headless page for room
    # status. Leave unset / "browser" for the original all-browser behavior.
    "mode": "browser",
    "page_size": 20,
    "seed_hotel_id": 1,
    "api_headed": False,  # API 模式浏览器是否显示窗口（首次过人机验证时可设 true）
    "api_workers": 8,  # API 模式每个 worker 内的并发线程数
    # International-site price fetch (hk.trip.com) — returns per-room prices in
    # HKD without login; prices are converted to CNY with a live rate.
    "intl_price": False,  # 是否抓国际版港币价格（默认关，开启后基本信息国内跑+价格国际版跑）
    "intl_workers": 4,  # 国际版价格抓取并发线程数
    "intl_headed": False,  # 国际版浏览器是否显示窗口
    "intl_proxy": None,  # 国际版专用代理；默认用 proxy
    # Optional proxies (e.g. http://ip:port). List fetch can rotate through
    # `proxy_list`; browser warmup uses `proxy`.
    "proxy": None,
    "proxy_list": [],
    "proxy_api_url": None,  # e.g. xiongmaodaili extract API returning {obj:[{ip,port}]}
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
