from __future__ import annotations

import argparse
import json
import shutil
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from ctrip_hotel.client import CtripHotelClient
from ctrip_hotel.config import EXAMPLE_CONFIG_PATH, ROOT, load_config
from ctrip_hotel.crawl_engine import (
    crawl_rooms_parallel,
    fetch_hotel_catalog,
    prepare_hotel_queue,
)
from ctrip_hotel.normalize import (
    build_hotel_document,
    normalize_hotels_from_dom,
    normalize_hotels_from_list_api,
    normalize_rooms,
)
from ctrip_hotel.state import load_done
from ctrip_hotel.store import new_run_dir, write_json


def cmd_init(_: argparse.Namespace) -> int:
    target = ROOT / "config.yaml"
    if target.exists():
        print(f"已存在 {target}")
        return 0
    shutil.copyfile(EXAMPLE_CONFIG_PATH, target)
    print(f"已创建 {target}，按需修改后运行: python -m ctrip_hotel crawl")
    return 0


def _latest_run(output_dir: Path) -> Path | None:
    marker = output_dir / "latest.json"
    if marker.exists():
        try:
            p = Path(json.loads(marker.read_text(encoding="utf-8"))["run_dir"])
            if (p / "catalog.json").exists():
                return p
        except Exception:
            pass
    runs = sorted(
        [p for p in output_dir.iterdir() if p.is_dir() and (p / "catalog.json").exists()],
        reverse=True,
    )
    return runs[0] if runs else None


def cmd_crawl(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config) if args.config else None)
    if args.headed is not None:
        cfg["headed"] = args.headed
        cfg["api_headed"] = args.headed
    if args.city_id is not None:
        cfg["city_id"] = args.city_id
    if args.max_hotels is not None:
        cfg["max_hotels"] = args.max_hotels
    if args.workers is not None:
        cfg["workers"] = args.workers
    if args.no_skip_done:
        cfg["skip_done"] = False
    if args.mode is not None:
        cfg["mode"] = args.mode
    if args.intl_price is not None:
        cfg["intl_price"] = args.intl_price

    run_dir = new_run_dir(cfg["output_dir"])
    write_json(run_dir / "config.used.json", cfg)
    workers = max(int(cfg.get("workers") or 1), 1)
    print(
        f"运行目录: {run_dir}\n"
        f"模式={cfg.get('mode', 'browser')} 城市={cfg['city_id']} {cfg.get('city_name', '')} "
        f"{cfg['check_in']}~{cfg['check_out']} | workers={workers} "
        f"skip_done={cfg.get('skip_done', True)}"
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
    print(f"列表 {len(hotels)} → 待抓 {len(todo)}（跳过 {len(skipped)}）")
    if not todo:
        print("没有待抓酒店。加 --no-skip-done 可强制重抓。")
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
    print(f"完成: {len(docs)} 酒店 / {rooms} 房型 / 图片URL {imgs} 条")
    print(f"结构化数据: {run_dir / 'hotels'}")
    print(f"预览: python -m ctrip_hotel preview")
    return 0


def cmd_diagnose(_: argparse.Namespace) -> int:
    cfg = load_config()
    print("诊断：抓 1 家完整结构（含图片 URL）…")
    try:
        if cfg.get("mode") == "api":
            from ctrip_hotel.api_client import (
                ApiRoomClient,
                build_fetch_result,
                extract_proxy_pool,
                fetch_hotel_list_pure,
                normalize_list_payloads,
            )

            proxies = extract_proxy_pool(cfg)
            items = fetch_hotel_list_pure(
                city_id=cfg["city_id"],
                check_in=cfg["check_in"],
                check_out=cfg["check_out"],
                pages=1,
                proxies=proxies,
            )
            hotels = normalize_list_payloads(items)
            if not hotels:
                print("未拿到列表")
                return 2
            h = hotels[0]
            print(f"诊断酒店: {h['hotel_id']} {h.get('name')}")
            with ApiRoomClient(cfg, seed_hotel_id=h["hotel_id"]) as client:
                room = client.fetch_room(h["hotel_id"])
                album = client.fetch_album(h["hotel_id"])
                additional = client.fetch_additional(h["hotel_id"])
                result = build_fetch_result(
                    hotel_id=h["hotel_id"],
                    hotel_meta=h,
                    room_payload=room,
                    album_payload=album,
                    additional_payload=additional,
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
        else:
            with CtripHotelClient(cfg, worker_id=None) as client:
                payloads, cards = client.fetch_hotel_list()
                hotels = []
                for p in payloads:
                    hotels.extend(normalize_hotels_from_list_api(p))
                if not hotels:
                    hotels = normalize_hotels_from_dom(cards)
                if not hotels:
                    print("未拿到列表")
                    return 2
                h = hotels[0]
                result = client.fetch_room_status(h["hotel_id"])
                doc = build_hotel_document(
                    hotel_meta=h,
                    page_hotel=result.get("page_hotel"),
                    fetch_result=result,
                    check_in=cfg["check_in"],
                    check_out=cfg["check_out"],
                )
            print(
                "hotel",
                doc["hotel"].get("name"),
                "imgs",
                len(doc["hotel"].get("images") or []),
                "rooms",
                len(doc["rooms"]),
            )
            if doc["rooms"]:
                r0 = doc["rooms"][0]
                print(
                    "room0",
                    r0.get("room_name"),
                    "imgs",
                    len(r0.get("images") or []),
                    "offers",
                    len(r0.get("offers") or []),
                    "categories",
                    len(r0.get("detail_categories") or []),
                )
            if not doc["rooms"]:
                return 3
    except RuntimeError as e:
        print(f"诊断失败: {e}")
        return 2
    print("诊断通过。")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    cfg = load_config()
    done = load_done(cfg["output_dir"])
    print(f"已完成记录: {len(done)}")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    cfg = load_config()
    run = Path(args.run) if args.run else _latest_run(Path(cfg["output_dir"]))
    if not run or not run.exists():
        print("没有可预览的数据，先跑: python -m ctrip_hotel crawl --no-skip-done --max-hotels 2")
        return 1
    preview_dir = ROOT / "preview"
    port = int(args.port or 8765)

    class Handler(SimpleHTTPRequestHandler):
        def translate_path(self, path: str) -> str:
            parsed = urlparse(path).path
            if parsed.startswith("/data/"):
                rel = parsed[len("/data/") :]
                return str((run / rel).resolve())
            # static preview
            rel = parsed.lstrip("/") or "index.html"
            return str((preview_dir / rel).resolve())

        def log_message(self, fmt: str, *a) -> None:
            print("[%s] %s" % (self.log_date_time_string(), fmt % a))

    print(f"还原预览: http://127.0.0.1:{port}/", flush=True)
    print(f"数据目录: {run}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ctrip_hotel",
        description="携程酒店结构化抓取（含图片）+ 还原预览",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="生成 config.yaml")
    p_init.set_defaults(func=cmd_init)

    p_crawl = sub.add_parser("crawl", help="抓取酒店+房型+图片URL")
    p_crawl.add_argument("--config", default=None)
    p_crawl.add_argument("--city-id", type=int, default=None)
    p_crawl.add_argument("--max-hotels", type=int, default=None)
    p_crawl.add_argument("--workers", type=int, default=None)
    p_crawl.add_argument("--headed", action=argparse.BooleanOptionalAction, default=None)
    p_crawl.add_argument("--no-skip-done", action="store_true")
    p_crawl.add_argument(
        "--mode",
        choices=["api", "browser"],
        default=None,
        help="抓取模式：api=纯HTTP列表+单页抓房态；browser=原浏览器方案",
    )
    p_crawl.add_argument(
        "--intl-price",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="抓国际版(hk.trip.com)港币价格并折算人民币（基本信息仍走国内版）",
    )
    p_crawl.set_defaults(func=cmd_crawl)

    p_diag = sub.add_parser("diagnose", help="检查结构是否完整")
    p_diag.set_defaults(func=cmd_diagnose)

    p_status = sub.add_parser("status", help="查看防重复记录")
    p_status.set_defaults(func=cmd_status)

    p_prev = sub.add_parser("preview", help="打开还原预览网站")
    p_prev.add_argument("--port", type=int, default=8765)
    p_prev.add_argument("--run", default=None, help="指定 data/时间戳 目录")
    p_prev.set_defaults(func=cmd_preview)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
