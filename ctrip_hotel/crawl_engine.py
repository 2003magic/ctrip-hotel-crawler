from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ctrip_hotel.api_client import (
    ApiRoomClient,
    build_fetch_result,
    extract_proxy_pool,
    fetch_hotel_list_pure,
    normalize_list_payloads,
)
from ctrip_hotel.client import CtripHotelClient, save_raw_payloads
from ctrip_hotel.completeness import document_gaps
from ctrip_hotel.normalize import (
    build_hotel_document,
    normalize_hotels_from_dom,
    normalize_hotels_from_list_api,
)
from ctrip_hotel.state import (
    dedupe_hotels,
    done_key,
    filter_new_hotels,
    load_done,
    mark_done,
    split_groups,
)
from ctrip_hotel.store import write_json, write_jsonl


def fetch_hotel_catalog(cfg: dict[str, Any], run_dir: Path) -> list[dict[str, Any]]:
    if cfg.get("mode") == "api":
        proxies = extract_proxy_pool(cfg)
        items = fetch_hotel_list_pure(
            city_id=cfg["city_id"],
            check_in=cfg["check_in"],
            check_out=cfg["check_out"],
            pages=int(cfg.get("pages") or 1),
            page_size=int(cfg.get("page_size") or 20),
            delay_ms=int(cfg.get("delay_ms") or 0),
            proxies=proxies,
        )
        hotels = normalize_list_payloads(items)
        if hotels:
            return dedupe_hotels(hotels)
        print("列表 API 未返回数据，回退到浏览器模式拉列表")
    with CtripHotelClient(cfg, worker_id=None) as client:
        list_payloads, dom_cards = client.fetch_hotel_list()
        save_raw_payloads(run_dir, "fetchHotelList", list_payloads)
        hotels: list[dict[str, Any]] = []
        for payload in list_payloads:
            hotels.extend(normalize_hotels_from_list_api(payload))
        if not hotels and dom_cards:
            print(f"列表 API 未返回数据，使用页面卡片 {len(dom_cards)} 条")
            hotels = normalize_hotels_from_dom(dom_cards)
    return dedupe_hotels(hotels)


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
            print(f"防重复: 跳过已抓 {len(skipped)} 家")
    max_hotels = cfg.get("max_hotels")
    if max_hotels:
        hotels = hotels[: int(max_hotels)]
    return hotels, skipped


def _worker_crawl(
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
    delay = int(cfg.get("delay_ms") or 1500) / 1000.0

    worker_cfg = dict(cfg)
    if cfg.get("workers_headed") is not None:
        worker_cfg["headed"] = bool(cfg.get("workers_headed"))
    if not worker_cfg.get("headed"):
        worker_cfg["verify_wait_sec"] = min(int(cfg.get("verify_wait_sec") or 30), 40)

    print(f"[w{worker_id}] 开始，本组 {len(hotels)} 家")
    with CtripHotelClient(worker_cfg, worker_id=worker_id) as client:
        for i, h in enumerate(hotels, 1):
            hid = h["hotel_id"]
            name = h.get("name") or ""
            print(f"[w{worker_id}] ({i}/{len(hotels)}) {hid} {name}")
            try:
                result = client.fetch_room_status(hid)
                doc = build_hotel_document(
                    hotel_meta=h,
                    page_hotel=result.get("page_hotel"),
                    fetch_result=result,
                    check_in=cfg["check_in"],
                    check_out=cfg["check_out"],
                )
                gaps = document_gaps(doc)
                # one retry if hotel static fields incomplete
                if gaps and any(
                    g in gaps
                    for g in (
                        "address",
                        "images",
                        "introduction",
                        "facilities/features",
                        "nearby",
                    )
                ):
                    print(f"[w{worker_id}]   retry gaps={gaps}")
                    time.sleep(1.2)
                    result = client.fetch_room_status(hid)
                    doc = build_hotel_document(
                        hotel_meta=h,
                        page_hotel=result.get("page_hotel"),
                        fetch_result=result,
                        check_in=cfg["check_in"],
                        check_out=cfg["check_out"],
                    )
                    gaps = document_gaps(doc)

                save_raw_payloads(worker_dir, f"room_{hid}", [result])
                write_json(hotels_dir / f"{hid}.json", doc)
                docs.append(doc)
                n_rooms = len(doc.get("rooms") or [])
                hotel = doc.get("hotel") or {}
                n_himgs = len(hotel.get("images") or [])
                if n_rooms == 0:
                    err_ids.append(hid)
                    print(f"[w{worker_id}]   ! no rooms gaps={gaps}")
                else:
                    ok_ids.append(hid)
                    print(
                        f"[w{worker_id}]   ok rooms={n_rooms} hotel_imgs={n_himgs} "
                        f"addr={'Y' if hotel.get('address') else 'N'} "
                        f"intro={'Y' if hotel.get('introduction') else 'N'} "
                        f"fac={len(hotel.get('facilities') or [])} "
                        f"nearby={sum(len(hotel.get('nearby',{}).get(k) or []) for k in ('metro','airport','train','other'))} "
                        f"gaps={gaps or 'none'} source={doc.get('source')}"
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
                print(f"[w{worker_id}]   !! {e}")
            if i < len(hotels) and delay > 0:
                time.sleep(delay)

    write_json(
        worker_dir / "summary.json",
        {"worker_id": worker_id, "ok": ok_ids, "err": err_ids, "docs": len(docs)},
    )
    print(f"[w{worker_id}] 结束 ok={len(ok_ids)} err={len(err_ids)}")
    return {
        "worker_id": worker_id,
        "docs": docs,
        "ok_ids": ok_ids,
        "err_ids": err_ids,
        "hotels": hotels,
    }


def _worker_crawl_api(
    worker_id: int,
    hotels: list[dict[str, Any]],
    cfg: dict[str, Any],
    run_dir: str,
) -> dict[str, Any]:
    """API-mode worker: one headless page per worker, serial in-page fetches.

    Each worker opens its own browser + page (warmup once), then replays room /
    album / additional requests inside the page for every assigned hotel.
    """
    run = Path(run_dir)
    worker_dir = run / "workers" / f"w{worker_id}"
    hotels_dir = run / "hotels"
    hotels_dir.mkdir(parents=True, exist_ok=True)
    worker_dir.mkdir(parents=True, exist_ok=True)

    docs: list[dict[str, Any]] = []
    ok_ids: list[Any] = []
    err_ids: list[Any] = []
    delay = int(cfg.get("delay_ms") or 800) / 1000.0

    print(f"[w{worker_id}] API 模式开始，本组 {len(hotels)} 家")
    with ApiRoomClient(cfg, worker_id=worker_id) as client:
        for i, h in enumerate(hotels, 1):
            hid = h["hotel_id"]
            name = h.get("name") or ""
            print(f"[w{worker_id}] ({i}/{len(hotels)}) {hid} {name}")
            try:
                room_payload = client.fetch_room(hid)
                album_payload = client.fetch_album(hid)
                additional_payload = client.fetch_additional(hid)
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
                    print(f"[w{worker_id}]   retry (no rooms) gaps={gaps}")
                    time.sleep(1.0)
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
                    print(f"[w{worker_id}]   ! no rooms gaps={gaps}")
                else:
                    ok_ids.append(hid)
                    print(
                        f"[w{worker_id}]   ok rooms={n_rooms} hotel_imgs={n_himgs} "
                        f"addr={'Y' if hotel.get('address') else 'N'} "
                        f"nearby={sum(len(hotel.get('nearby',{}).get(k) or []) for k in ('metro','airport','train','other'))} "
                        f"gaps={gaps or 'none'} source={doc.get('source')}"
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
                print(f"[w{worker_id}]   !! {e}")
            if i < len(hotels) and delay > 0:
                time.sleep(delay)

    write_json(
        worker_dir / "summary.json",
        {"worker_id": worker_id, "ok": ok_ids, "err": err_ids, "docs": len(docs)},
    )
    print(f"[w{worker_id}] 结束 ok={len(ok_ids)} err={len(err_ids)}")
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
    workers = max(int(cfg.get("workers") or 1), 1)
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
        print(f"分组 w{i}: {len(g)} 家 -> {[h['hotel_id'] for h in g]}")

    all_docs: list[dict[str, Any]] = []
    crawled_hotels: list[dict[str, Any]] = []

    worker_fn = _worker_crawl_api if cfg.get("mode") == "api" else _worker_crawl

    if len(jobs) == 1:
        res = worker_fn(jobs[0][0], jobs[0][1], cfg, str(run_dir))
        all_docs.extend(res["docs"])
        crawled_hotels.extend(res["hotels"])
    else:
        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            futs = {
                ex.submit(worker_fn, wid, group, cfg, str(run_dir)): wid
                for wid, group in jobs
            }
            for fut in as_completed(futs):
                res = fut.result()
                all_docs.extend(res["docs"])
                crawled_hotels.extend(res["hotels"])

    # catalog for preview
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
                "file": f"hotels/{d.get('hotel_id')}.json",
            }
        )
    write_json(run_dir / "catalog.json", catalog)
    write_jsonl(run_dir / "catalog.jsonl", catalog)
    return crawled_hotels, all_docs
