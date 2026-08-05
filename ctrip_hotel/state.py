from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterable

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
