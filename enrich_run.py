#!/usr/bin/env python3
"""补全已有 run：国内 additional（简介/设施/附近）+ 国际版港币价→人民币。"""

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
    p.add_argument("--run", required=True)
    p.add_argument("--proxy", default="http://127.0.0.1:7897")
    p.add_argument("--intl-workers", type=int, default=2)
    p.add_argument("--skip-intl", action="store_true")
    p.add_argument("--skip-static", action="store_true")
    args = p.parse_args()

    from ctrip_hotel.api_client import ApiRoomClient, adapt_album_to_legacy
    from ctrip_hotel.config import load_config
    from ctrip_hotel.hotel_parse import (
        facilities_from_additional,
        images_from_album,
        introduction_from_additional,
        nearby_from_additional,
    )
    from ctrip_hotel.intl_client import (
        IntlRoomClient,
        _payload_kind,
        fetch_hkd_cny_rate,
        merge_prices_into_doc,
        normalize_intl_prices,
    )
    from ctrip_hotel.store import write_json

    run = Path(args.run)
    hotels_dir = run / "hotels"
    files = sorted(hotels_dir.glob("*.json"), key=lambda x: int(x.stem))
    if not files:
        print("no hotels")
        return 1

    cfg = load_config()
    cfg["api_headed"] = False
    cfg["intl_headed"] = True
    cfg["intl_proxy"] = args.proxy
    cfg["intl_workers"] = args.intl_workers
    cfg["intl_http_retries"] = 2
    # Prefer seeds that reliably fire H5 additional/album.
    preferred = [80920781, 40611324, 1286148]
    seeds = [x for x in preferred if x in {int(f.stem) for f in files}]
    seeds.extend(int(f.stem) for f in files if int(f.stem) not in seeds)
    cfg["seed_hotel_id"] = seeds[0]
    cfg["seed_hotel_ids"] = seeds[1:5]

    ids = [int(f.stem) for f in files]
    print(f"enrich {len(ids)} hotels @ {run}", flush=True)

    # --- 1) static: additional + album ---
    if not args.skip_static:
        print("\n[1/2] domestic additional/album…", flush=True)
        t0 = time.perf_counter()
        ok_static = 0
        with ApiRoomClient(cfg, seed_hotel_id=ids[0]) as client:
            if not client._additional_template:
                print("  WARN: additional template missing — 简介/设施/附近可能补不全", flush=True)
            for i, hid in enumerate(ids, 1):
                doc = json.loads((hotels_dir / f"{hid}.json").read_text(encoding="utf-8"))
                hotel = dict(doc.get("hotel") or {})
                try:
                    add = client.fetch_additional(hid) if client._additional_template else {}
                    alb = client.fetch_album(hid) if client._album_template else {}
                except Exception as e:
                    print(f"  FAIL {hid} static {e}", flush=True)
                    continue
                if add:
                    intro = introduction_from_additional(add)
                    fac = facilities_from_additional(add)
                    nearby = nearby_from_additional(add)
                    if intro:
                        hotel["introduction"] = intro
                    if fac:
                        hotel["facilities"] = fac
                        if not hotel.get("features"):
                            hotel["features"] = [{"name": x["name"]} for x in fac[:12]]
                    if any(nearby.get(k) for k in ("metro", "airport", "train", "other")):
                        hotel["nearby"] = nearby
                    tips = ((add.get("data") or {}).get("hotelReservationTips") or {}).get(
                        "tipList"
                    )
                    if tips:
                        hotel["tips"] = tips
                if alb:
                    legacy = adapt_album_to_legacy(alb)
                    imgs, total = images_from_album(legacy)
                    if imgs:
                        hotel["images"] = imgs
                        hotel["image_count"] = total or len(imgs)
                doc["hotel"] = hotel
                write_json(hotels_dir / f"{hid}.json", doc)
                ok_static += 1
                if i <= 3 or i % 20 == 0:
                    print(
                        f"  {i}/{len(ids)} {hid} intro={'Y' if hotel.get('introduction') else 'N'} "
                        f"fac={len(hotel.get('facilities') or [])} "
                        f"nearby={sum(len((hotel.get('nearby') or {}).get(k) or []) for k in ('metro','airport','train','other'))}",
                        flush=True,
                    )
        print(f"  static done {ok_static}/{len(ids)} in {time.perf_counter()-t0:.1f}s", flush=True)

    # --- 2) intl prices ---
    if not args.skip_intl:
        need = []
        for hid in ids:
            doc = json.loads((hotels_dir / f"{hid}.json").read_text(encoding="utf-8"))
            h = doc.get("hotel") or {}
            if h.get("min_price_cny") is None:
                need.append(hid)
        print(f"\n[2/2] intl prices todo={len(need)}…", flush=True)
        if need:
            rate = fetch_hkd_cny_rate()
            print(f"  rate HKD→CNY={rate:.4f}", flush=True)
            t1 = time.perf_counter()
            merged = 0
            with IntlRoomClient(cfg, seed_hotel_id=need[0]) as intl:
                # small waves via client batch
                payloads = intl.fetch_room_batch(need, max_workers=args.intl_workers)
            for hid, payload in zip(need, payloads):
                if _payload_kind(payload) != "ok":
                    continue
                pi = normalize_intl_prices(
                    payload,
                    check_in=cfg["check_in"],
                    check_out=cfg["check_out"],
                    rate=rate,
                )
                if not pi.get("rooms"):
                    continue
                doc = json.loads((hotels_dir / f"{hid}.json").read_text(encoding="utf-8"))
                doc = merge_prices_into_doc(doc, pi)
                write_json(hotels_dir / f"{hid}.json", doc)
                merged += 1
            print(
                f"  intl merged {merged}/{len(need)} in {time.perf_counter()-t1:.1f}s",
                flush=True,
            )

    # catalog min prices
    cat_path = run / "catalog.json"
    if cat_path.exists():
        cat = json.loads(cat_path.read_text(encoding="utf-8"))
        by_id = {}
        for item in cat:
            if isinstance(item, dict) and item.get("hotel_id") is not None:
                by_id[int(item["hotel_id"])] = item
        for hid in ids:
            doc = json.loads((hotels_dir / f"{hid}.json").read_text(encoding="utf-8"))
            h = doc.get("hotel") or {}
            if hid in by_id:
                if h.get("min_price_cny") is not None:
                    by_id[hid]["min_price_cny"] = h["min_price_cny"]
                if h.get("min_price_hkd") is not None:
                    by_id[hid]["min_price_hkd"] = h["min_price_hkd"]
        write_json(cat_path, cat)
        print("catalog updated", flush=True)

    priced = sum(
        1
        for hid in ids
        if (json.loads((hotels_dir / f"{hid}.json").read_text(encoding="utf-8")).get("hotel") or {}).get(
            "min_price_cny"
        )
        is not None
    )
    intro_n = sum(
        1
        for hid in ids
        if (json.loads((hotels_dir / f"{hid}.json").read_text(encoding="utf-8")).get("hotel") or {}).get(
            "introduction"
        )
    )
    print(f"\nsummary priced_cny={priced}/{len(ids)} with_intro={intro_n}/{len(ids)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
