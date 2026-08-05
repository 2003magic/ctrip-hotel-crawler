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
        date.fromisoformat(check_in) + timedelta(days=1)
    ).isoformat()
    cfg["check_in"] = check_in
    cfg["check_out"] = check_out
    cfg["output_dir"] = str((ROOT / cfg["output_dir"]).resolve())
    return cfg
