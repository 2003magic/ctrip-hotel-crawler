"""Persist Playwright storage_state across runs (hybrid browser+HTTP).

Learned from:
  - Playwright auth docs (storage_state reuse)
  - goofish-scrape / ScrapingCentral hybrid pattern (browser once, HTTP many)
  - Camoufox persistent_context idea (cookies survive between launches)

phantom-token is short-lived and NOT persisted. We persist cookies/localStorage
so the next warmup navigation fires room XHR faster and often skips captcha.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ctrip_hotel.config import ROOT

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
