#!/usr/bin/env python3
"""API 模式一键验证脚本。

用法：
    python api_verify.py                # 用 config.yaml 或默认配置验证
    python api_verify.py --city-id 2    # 指定城市
    python api_verify.py --headed       # 显示浏览器窗口（首次建议）

它会：
  1. 用纯 HTTP 拉酒店列表（curl_cffi 模拟 Chrome TLS 指纹）
  2. 打开一个无头浏览器预热，捕获房态请求模板
  3. 抓取前 2 家酒店的完整房态（房型/床型/面积/早餐/取消/图片/周边）
  4. 打印数据完整性报告

如果第 2 步失败（提示"未捕获到房态请求模板"），说明当前网络/IP 被携程
临时风控。解决：
  - 加 --headed 手动打开窗口过一次人机验证
  - 稍等几分钟重试
  - 在 config.yaml 配置代理池（proxy / proxy_list / proxy_api_url）
"""

import argparse
import os
import sys
from pathlib import Path

# Allow running from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS", "1")


def main() -> int:
    p = argparse.ArgumentParser(description="API 模式一键验证")
    p.add_argument("--city-id", type=int, default=None)
    p.add_argument("--max-hotels", type=int, default=2)
    p.add_argument("--headed", action="store_true", default=False)
    p.add_argument("--no-proxy", action="store_true", help="忽略配置中的代理")
    args = p.parse_args()

    from ctrip_hotel.config import load_config
    from ctrip_hotel.api_client import (
        ApiRoomClient,
        build_fetch_result,
        extract_proxy_pool,
        fetch_hotel_list_pure,
        normalize_list_payloads,
    )
    from ctrip_hotel.normalize import build_hotel_document
    from ctrip_hotel.completeness import document_gaps

    cfg = load_config()
    cfg["mode"] = "api"
    if args.city_id:
        cfg["city_id"] = args.city_id
    if args.headed:
        cfg["api_headed"] = True
    if args.no_proxy:
        cfg["proxy"] = None
        cfg["proxy_list"] = []
        cfg["proxy_api_url"] = None

    print("=" * 60)
    print(f"API 模式验证 | 城市 {cfg['city_id']} | "
          f"{cfg['check_in']} ~ {cfg['check_out']} | headed={cfg.get('api_headed')}")
    print("=" * 60)

    # 1. 列表（纯 HTTP）
    print("\n[1/3] 拉取酒店列表（纯 HTTP）…")
    proxies = extract_proxy_pool(cfg)
    if proxies:
        print(f"      使用代理池 {len(proxies)} 个")
    items = fetch_hotel_list_pure(
        city_id=cfg["city_id"],
        check_in=cfg["check_in"],
        check_out=cfg["check_out"],
        pages=1,
        proxies=proxies,
    )
    hotels = normalize_list_payloads(items)
    print(f"      列表 {len(hotels)} 家")
    if not hotels:
        print("      ✗ 列表为空。网络或接口异常。")
        return 2
    for h in hotels[:3]:
        print(f"        {h['hotel_id']} {h['name'][:30]} 星={h['star']} 分={h['score']}")

    # 2. 预热 + 抓房态
    print("\n[2/3] 打开浏览器预热并抓取房态…")
    target = hotels[: args.max_hotels]
    seed = target[0]["hotel_id"]
    print(f"      预热 seed={seed}")
    try:
        with ApiRoomClient(cfg, seed_hotel_id=seed) as client:
            for h in target:
                hid = h["hotel_id"]
                room = client.fetch_room(hid)
                album = client.fetch_album(hid)
                additional = client.fetch_additional(hid)
                result = build_fetch_result(
                    hotel_id=hid, hotel_meta=h, room_payload=room,
                    album_payload=album, additional_payload=additional,
                    check_in=cfg["check_in"], check_out=cfg["check_out"],
                )
                doc = build_hotel_document(
                    hotel_meta=h, page_hotel=result.get("page_hotel"),
                    fetch_result=result, check_in=cfg["check_in"],
                    check_out=cfg["check_out"],
                )
                gaps = document_gaps(doc)
                hotel = doc["hotel"]
                ok = len(doc["rooms"]) > 0
                print(f"      {'✓' if ok else '✗'} {hid} {h['name'][:25]}")
                print(f"          房型={len(doc['rooms'])} 图片={len(hotel.get('images') or [])} "
                      f"周边={sum(len(hotel.get('nearby',{}).get(k) or []) for k in ('metro','airport','train','other'))} "
                      f"gaps={gaps or '无'}")
                if doc["rooms"]:
                    r0 = doc["rooms"][0]
                    print(f"          示例房型: {r0.get('room_name')} | 床={r0.get('bed')} | "
                          f"窗={r0.get('window')} | 面积={r0.get('area')} | offers={len(r0.get('offers') or [])}")
    except RuntimeError as e:
        print(f"\n  ✗ 预热失败: {e}")
        print("  提示：这是网络/IP 被携程临时风控。加 --headed 手动过验证，或配置代理，或稍后重试。")
        return 3

    # 3. 汇总
    print("\n[3/3] 完成！")
    print("  正式抓取: python -m ctrip_hotel crawl --mode api")
    return 0


if __name__ == "__main__":
    sys.exit(main())
