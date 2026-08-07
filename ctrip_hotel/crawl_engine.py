from __future__ import annotations

import threading
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
from ctrip_hotel.intl_client import (
    EXCHANGE_FALLBACK,
    IntlRoomClient,
    fetch_hkd_cny_rate,
    merge_prices_into_doc,
    normalize_intl_prices,
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
        # Avoid fetching full `pages` when max_hotels is smaller (big wall-time win).
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

    # FX rate fetch overlaps with headed Edge warmup (independent network path).
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
        f"[intl] HTTP 完成 {len(hotels)} 家 耗时 {time.time() - t0:.1f}s (汇率 {rate:.4f})",
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
    print(f"[intl] 价格合并 {merged}/{len(hotels)} 家", flush=True)
    return docs


def _merge_intl_prices(
    docs: list[dict[str, Any]],
    hotels: list[dict[str, Any]],
    cfg: dict[str, Any],
    run_dir: Path,
) -> list[dict[str, Any]]:
    """One shared IntlRoomClient for the whole run (avoids N headed Edge warmups)."""
    if not cfg.get("intl_price") or not hotels:
        return docs
    t0 = time.time()
    try:
        payloads, rate = _fetch_intl_payloads(hotels, cfg, run_dir)
        docs = _apply_intl_payloads(docs, hotels, payloads, rate, cfg, run_dir)
        print(f"[intl] 总耗时 {time.time() - t0:.1f}s", flush=True)
    except Exception as e:
        print(f"[intl] 价格抓取失败: {e}", flush=True)
    return docs


def _worker_crawl_api(
    worker_id: int,
    hotels: list[dict[str, Any]],
    cfg: dict[str, Any],
    run_dir: str,
) -> dict[str, Any]:
    """API-mode worker: warmup once, then fetch rooms via PURE HTTP concurrently.

    A single headless page is used only to obtain a valid phantom-token + cookies
    (and optionally album/additional templates). After warmup, room status for all
    hotels is fetched with curl_cffi in a thread pool — no browser per hotel.

    album / additional prefer pure HTTP (parallel); fall back to in-page replay.
    International price merge runs once after all API workers (see crawl_rooms_parallel).
    """
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

    print(f"[w{worker_id}] API 模式开始，本组 {len(hotels)} 家 (并发 {max_workers})")
    t_warm = time.time()
    # __enter__ 内：捕获模板 → 真实 HTTP 探针验活 → 失败则抛错，绝不开跑批量。
    try:
        client_ctx = ApiRoomClient(cfg, worker_id=worker_id)
    except Exception as e:
        print(f"[w{worker_id}] 预热客户端创建失败，已中止: {e}", flush=True)
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
            f"[w{worker_id}] rooms HTTP {len(hotels)} 家 {time.time() - t_rooms:.1f}s",
            flush=True,
        )
        t_enrich = time.time()
        enrich = client.fetch_enrich_batch(hotel_ids, max_workers=max_workers)
        print(
            f"[w{worker_id}] album/additional {len(hotels)} 家 {time.time() - t_enrich:.1f}s",
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
            # API 批量已完成，此处不再按 delay_ms 空等（那是浏览器串行模式用的）

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
    t_all = time.time()
    # API 模式：房态是纯 HTTP 并发，浏览器只用于一次 token 预热。
    # 多 worker 只会重复开浏览器，不提速还浪费（尤其 intl headed Edge）。
    if cfg.get("mode") == "api":
        workers = 1
    else:
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
    parallel_intl = (
        cfg.get("mode") == "api"
        and cfg.get("intl_price")
        and bool(hotels)
    )

    intl_payloads: list[dict[str, Any]] | None = None
    intl_rate = 0.0
    intl_error: Exception | None = None

    if parallel_intl:
        # 实测：与国内 warmup 同时启动总墙钟更短；错开启动会把 intl 整段延后。
        print("[crawl] 国内 + 国际价并行…", flush=True)
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
            print(f"[intl] 价格抓取失败: {intl_error}", flush=True)
        elif intl_payloads is not None:
            all_docs = _apply_intl_payloads(
                all_docs, crawled_hotels, intl_payloads, intl_rate, cfg, run_dir
            )
    elif len(jobs) == 1:
        res = worker_fn(jobs[0][0], jobs[0][1], cfg, str(run_dir))
        all_docs.extend(res["docs"])
        crawled_hotels.extend(res["hotels"])
        if cfg.get("mode") == "api" and cfg.get("intl_price"):
            all_docs = _merge_intl_prices(all_docs, crawled_hotels, cfg, run_dir)
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
        if cfg.get("mode") == "api" and cfg.get("intl_price"):
            all_docs = _merge_intl_prices(all_docs, crawled_hotels, cfg, run_dir)

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
                "min_price_hkd": h.get("min_price_hkd"),
                "min_price_cny": h.get("min_price_cny"),
                "file": f"hotels/{d.get('hotel_id')}.json",
            }
        )
    write_json(run_dir / "catalog.json", catalog)
    write_jsonl(run_dir / "catalog.jsonl", catalog)
    print(f"[crawl] 总耗时 {time.time() - t_all:.1f}s", flush=True)
    return crawled_hotels, all_docs
