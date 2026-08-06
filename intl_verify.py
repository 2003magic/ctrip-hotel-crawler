#!/usr/bin/env python3
"""国际版（hk.trip.com）港币价格一键验证脚本（API 模式）。

机制（2026-08 实测）：
- phantom-token = window.signature(精确 POST body)，一次性且绑定 hotelId
- 浏览器只负责预热+签名；房价走 curl_cffi 纯 HTTP
- 建议 --proxy 走 Clash 等境外出口；签名浏览器建议有头（默认）

用法：
    python intl_verify.py --proxy http://127.0.0.1:7897
    python intl_verify.py --proxy http://127.0.0.1:7897 --max-hotels 3
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS", "1")


def main() -> int:
    p = argparse.ArgumentParser(description="国际版港币价格验证")
    p.add_argument("--city-id", type=int, default=None)
    p.add_argument("--max-hotels", type=int, default=2)
    p.add_argument("--proxy", default=None, help="Clash 等本地代理, 如 http://127.0.0.1:7897")
    p.add_argument("--headed", action="store_true", default=True)
    p.add_argument("--headless", action="store_true", default=False, help="强制无头（易 430）")
    args = p.parse_args()

    from ctrip_hotel.config import load_config
    from ctrip_hotel.api_client import (
        fetch_hotel_list_pure,
        normalize_list_payloads,
    )
    from ctrip_hotel.intl_client import (
        IntlRoomClient,
        fetch_hkd_cny_rate,
        normalize_intl_prices,
    )

    cfg = load_config()
    cfg["mode"] = "api"
    if args.city_id:
        cfg["city_id"] = args.city_id
    if args.proxy:
        cfg["intl_proxy"] = args.proxy
        cfg["proxy"] = args.proxy
    cfg["intl_headed"] = False if args.headless else True

    print("=" * 64)
    print(
        f"国际版港币价格验证 | 城市 {cfg['city_id']} | {cfg['check_in']}~{cfg['check_out']}"
        f" | proxy={args.proxy or '(无)'} | headed={cfg['intl_headed']}"
    )
    print("=" * 64)

    print("\n[1/4] 拉取国内酒店列表（纯 HTTP）...")
    items = fetch_hotel_list_pure(
        city_id=cfg["city_id"],
        check_in=cfg["check_in"],
        check_out=cfg["check_out"],
        pages=1,
        page_size=20,
    )
    hotels = normalize_list_payloads(items)
    print(f"      列表 {len(hotels)} 家")
    if not hotels:
        print("      列表为空，无法继续")
        return 2
    for h in hotels[:3]:
        print(f"        {h['hotel_id']} {h['name'][:30]}")

    print("\n[2/4] 获取港币->人民币汇率...")
    rate = fetch_hkd_cny_rate()
    print(f"      汇率 HKD->CNY = {rate:.4f}")

    target = hotels[: args.max_hotels]
    seed = target[0]["hotel_id"]
    print(f"\n[3/4] 国际版预热+签名 seed={seed}（数据走纯 HTTP）...")
    try:
        with IntlRoomClient(cfg, seed_hotel_id=seed) as client:
            payloads = client.fetch_room_batch(
                [h["hotel_id"] for h in target],
                max_workers=min(4, max(len(target), 1)),
            )
    except RuntimeError as e:
        print(f"\n  FAIL 预热失败: {e}")
        print("  提示：请加 --proxy（如 Clash 7897），并保持有头模式（不要 --headless）。")
        return 3

    print("\n[4/4] 价格结构：")
    out_dir = Path("data")
    out_dir.mkdir(exist_ok=True)
    ok_n = 0
    for h, payload in zip(target, payloads):
        hid = h["hotel_id"]
        name = (h.get("name") or "")[:25]
        if not payload or payload.get("error"):
            print(f"\n  FAIL {hid} {name} 请求失败: {payload.get('error') if payload else 'empty'}")
            continue
        data = payload.get("data") or {}
        if data.get("htlSpiderActionErrorCode"):
            print(
                f"\n  FAIL {hid} 风控码 {data.get('htlSpiderActionErrorCode')}"
                f"（4030=token 失效/复用；430=会话/IP 被拦）"
            )
            continue
        pi = normalize_intl_prices(
            payload,
            check_in=cfg["check_in"],
            check_out=cfg["check_out"],
            rate=rate,
        )
        if not pi.get("rooms"):
            print(f"\n  FAIL {hid} 无价格数据")
            raw_path = out_dir / f"intl_raw_{hid}.json"
            raw_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            print(f"    原始响应已存 {raw_path}")
            continue
        ok_n += 1
        start = pi["rooms"][0].get("start_price_hkd")
        print(f"\n  OK {hid} {name}  起价 HK${start} ≈¥{pi['rooms'][0].get('start_price_cny')}")
        for r in (pi.get("rooms") or [])[:5]:
            print(
                f"    [总房型] {r.get('room_name')}  "
                f"HK${r.get('start_price_hkd')} ≈¥{r.get('start_price_cny')}"
            )
            for pl in (r.get("plans") or [])[:3]:
                fold = " [折叠]" if pl.get("folded") else ""
                print(
                    f"      - {pl.get('display_price') or pl.get('price_hkd')} "
                    f"≈¥{pl.get('price_cny')}{fold}"
                )
                print(f"        早餐: {pl.get('meal')} | 取消: {pl.get('cancel')}")

    print(f"\n完成！成功 {ok_n}/{len(target)}")
    return 0 if ok_n == len(target) else 4


if __name__ == "__main__":
    sys.exit(main())
