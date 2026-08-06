#!/usr/bin/env python3
"""Merge international HKD prices into an existing domestic run (补国内版)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True, help="data/<timestamp> run dir")
    p.add_argument("--proxy", default="http://127.0.0.1:7897")
    p.add_argument("--chunk", type=int, default=20)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--seed", type=int, default=None)
    args = p.parse_args()

    from ctrip_hotel.config import load_config
    from ctrip_hotel.intl_client import (
        IntlRoomClient,
        fetch_hkd_cny_rate,
        merge_prices_into_doc,
        normalize_intl_prices,
        _payload_kind,
    )
    from ctrip_hotel.store import write_json

    run = Path(args.run)
    hotels_dir = run / "hotels"
    files = sorted(hotels_dir.glob("*.json"), key=lambda x: int(x.stem))
    if not files:
        print(f"no hotels in {hotels_dir}")
        return 1

    cfg = load_config()
    cfg["intl_price"] = True
    cfg["intl_headed"] = True
    cfg["intl_proxy"] = args.proxy
    cfg["intl_workers"] = args.workers
    cfg["intl_http_retries"] = 2
    cfg["intl_rewarm_after"] = 2

    ids: list[int] = []
    already = 0
    for f in files:
        hid = int(f.stem)
        doc = json.loads(f.read_text(encoding="utf-8"))
        hotel = doc.get("hotel") or {}
        if hotel.get("price_info") and hotel.get("min_price_hkd") is not None:
            already += 1
            continue
        ids.append(hid)
    seed = args.seed or (ids[0] if ids else int(files[0].stem))
    rate = fetch_hkd_cny_rate()
    print(
        f"run={run} todo={len(ids)} already={already} seed={seed} rate={rate:.4f}",
        flush=True,
    )
    if not ids:
        print("nothing to merge")
        return 0

    t0 = time.perf_counter()
    merged = 0
    failed = 0
    with IntlRoomClient(cfg, seed_hotel_id=seed) as client:
        for i in range(0, len(ids), args.chunk):
            chunk = ids[i : i + args.chunk]
            print(f"\n[{i+1}-{i+len(chunk)}/{len(ids)}] fetch…", flush=True)
            t1 = time.perf_counter()
            payloads = client.fetch_room_batch(chunk, max_workers=args.workers)
            print(f"  http done in {time.perf_counter()-t1:.1f}s", flush=True)
            for hid, payload in zip(chunk, payloads):
                kind = _payload_kind(payload)
                if kind != "ok":
                    failed += 1
                    err = (payload or {}).get("error") or (
                        (payload or {}).get("data") or {}
                    ).get("htlSpiderActionErrorCode")
                    print(f"  FAIL {hid} kind={kind} {err}", flush=True)
                    continue
                pi = normalize_intl_prices(
                    payload,
                    check_in=cfg["check_in"],
                    check_out=cfg["check_out"],
                    rate=rate,
                )
                if not pi.get("rooms"):
                    failed += 1
                    print(f"  FAIL {hid} no priced rooms", flush=True)
                    continue
                doc = json.loads(
                    (hotels_dir / f"{hid}.json").read_text(encoding="utf-8")
                )
                doc = merge_prices_into_doc(doc, pi)
                write_json(hotels_dir / f"{hid}.json", doc)
                merged += 1
                if merged <= 5 or merged % 10 == 0:
                    print(
                        f"  OK {hid} min_hkd={doc['hotel'].get('min_price_hkd')} "
                        f"priced_phys={len(pi['rooms'])}",
                        flush=True,
                    )

    print(
        f"\ndone merged={merged}/{len(ids)} failed={failed} "
        f"in {time.perf_counter()-t0:.1f}s",
        flush=True,
    )
    return 0 if merged else 2


if __name__ == "__main__":
    sys.exit(main())
