#!/usr/bin/env python3
"""国际版（hk.trip.com）港币价格一键验证脚本。

在【你自己电脑】上跑（家宽 IP / 挂了 Clash 的本地环境），因为：
- 国际版 getHotelRoomListOversea 对数据中心 IP 有风控（430 whaleguard block / 4030）
- 你家宽 IP 或 Clash 代理出口能正常访问 hk.trip.com

用法：
    python intl_verify.py                # 验证前 2 家
    python intl_verify.py --max-hotels 3
    python intl_verify.py --city-id 2
    python intl_verify.py --proxy http://127.0.0.1:7897   # 走 Clash 本地代理

它会：
  1. 纯 HTTP 拉国内酒店列表（curl_cffi 模拟 Chrome）
  2. 打开 hk.trip.com 预热，捕获 getHotelRoomListOversea 请求模板
  3. 纯 HTTP 重放抓前 N 家的港币价格
  4. 按实时汇率折算人民币，打印"总房型 → 子房型(方案)"价格结构
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
    p.add_argument("--headed", action="store_true", default=False)
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
    if args.headed:
        cfg["intl_headed"] = True

    print("=" * 64)
    print(f"国际版港币价格验证 | 城市 {cfg['city_id']} | {cfg['check_in']}~{cfg['check_out']}"
          f" | proxy={args.proxy or '(无)'}")
    print("=" * 64)

    # 1. 国内列表（纯 HTTP）
    print("\n[1/4] 拉取国内酒店列表（纯 HTTP）…")
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
        print("      ✗ 列表为空，无法继续")
        return 2
    for h in hotels[:3]:
        print(f"        {h['hotel_id']} {h['name'][:30]}")

    # 2. 汇率
    print("\n[2/4] 获取港币→人民币汇率…")
    rate = fetch_hkd_cny_rate()
    print(f"      汇率 HKD→CNY = {rate:.4f}")

    # 3. 国际版预热 + 抓价格
    target = hotels[: args.max_hotels]
    seed = target[0]["hotel_id"]
    print(f"\n[3/4] 国际版预热 seed={seed}（hk.trip.com）…")
    try:
        with IntlRoomClient(cfg, seed_hotel_id=seed) as client:
            payloads = client.fetch_room_batch(
                [h["hotel_id"] for h in target], max_workers=1
            )
    except RuntimeError as e:
        print(f"\n  ✗ 预热失败: {e}")
        print("  提示：国际版对数据中心 IP 风控，请在【自己电脑】上跑，"
              "或用 --proxy 指定 Clash 代理。")
        return 3

    # 4. 解析输出
    print("\n[4/4] 价格结构：")
    for h, payload in zip(target, payloads):
        hid = h["hotel_id"]
        if not payload or payload.get("error"):
            print(f"\n  ✗ {hid} {h['name'][:25]} 请求失败: {payload.get('error') if payload else 'empty'}")
            continue
        data = (payload.get("data") or {})
        if data.get("htlSpiderActionErrorCode"):
            print(f"\n  ✗ {hid} 风控码 {data.get('htlSpiderActionErrorCode')}（4030 表示被风控）")
            continue
        pi = normalize_intl_prices(payload, check_in=cfg["check_in"],
                                   check_out=cfg["check_out"], rate=rate)
        if not pi.get("rooms"):
            print(f"\n  ✗ {hid} 无价格数据（接口可能返回空/结构变化）")
            # 存原始响应便于排查
            with open(f"/tmp/intl_raw_{hid}.json", "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            print(f"    原始响应已存 /tmp/intl_raw_{hid}.json")
            continue
        print(f"\n  ✓ {hid} {h['name'][:25]}  起价 HK${pi.get('rooms',[{}])[0].get('start_price_hkd')}")
        for r in pi.get("rooms") or []:
            print(f"    [总房型] {r.get('room_name')}  HK${r.get('start_price_hkd')} ≈¥{r.get('start_price_cny')}")
            for pl in r.get("plans") or []:
                fold = " [折叠]" if pl.get("folded") else ""
                print(f"      - {pl.get('room_name')}  HK${pl.get('price_hkd')} ≈¥{pl.get('price_cny')}"
                      f"{fold}")
                print(f"        摘要: {pl.get('summary')} | 早餐: {pl.get('meal')} | 取消: {pl.get('cancel')}")

    print("\n完成！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
