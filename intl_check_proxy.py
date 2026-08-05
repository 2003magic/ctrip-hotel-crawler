#!/usr/bin/env python3
"""国际版代理体检脚本。

检查每个代理的出口 IP 归属，判断是否"境外住宅 IP"（whaleguard 只放行这类）。
数据中心 / 机房 / 商务段 IP 一律 4030。

用法：
    python intl_check_proxy.py                     # 检查 config.yaml 里的代理池
    python intl_check_proxy.py --proxy http://1.2.3.4:8080   # 检查单个代理
    python intl_check_proxy.py --proxy http://1.2.3.4:8080 --test   # 并直接跑国际版验证
    python intl_check_proxy.py --test              # 对 config 里每个代理跑验证

判定：
    住宅 = country 非 CN 且 org 不含 hosting/datacenter/cloud/com 等机房关键词
    逐房型价格能拿到才算真正可用；出口 IP 干净只是必要条件。
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS", "1")

# ip-api 免费版返回的组织/类型信息（必须显式 fields 才会带 proxy/hosting 标记）
IP_API = ("http://ip-api.com/json/{}"
          "?fields=status,message,country,countryCode,regionName,city,"
          "isp,org,as,asname,mobile,proxy,hosting")

# 机房/托管特征词（出现即判定非住宅）
_DC_KEYWORDS = (
    "hosting", "datacenter", "data center", "cloud", "as14061", "ovh",
    "digitalocean", "linode", "vultr", "amazon", "microsoft", "google",
    "alibaba", "tencent", "huawei", "choopa", "multacom", "quadranet",
    "rackspace", "hetzner", "contabo", "xtom", "psychz", "zhanxia",
    "bandwagon", "搬瓦工", "shaofeng", "idc", "机房", "gcore", "m247",
    "leaseweb", "keycdn", "ipvolume", "reliablesite", "cogent",
)


def exit_ip(proxy: str | None, timeout: int = 12) -> str | None:
    """通过代理查出口 IP。proxy=None 时直连。"""
    import curl_cffi.requests as cffi_requests
    for url in ("https://api.ipify.org?format=json", "http://ip.3322.net"):
        try:
            kwargs: dict = {"timeout": timeout, "impersonate": "chrome"}
            if proxy:
                kwargs["proxies"] = {"http": proxy, "https": proxy}
            r = cffi_requests.get(url, **kwargs)
            txt = r.text.strip()
            if url.endswith("ipify.org"):
                return json.loads(txt).get("ip")
            if txt and txt[0].isdigit():
                return txt
        except Exception:
            continue
    return None


def classify(ip: str) -> dict:
    """查 IP 归属并分类。

    决定性依据是 ip-api 的 proxy / hosting 布尔字段（机房/代理段会被标记）。
    住宅判定 = 非 CN 且 非 proxy 且 非 hosting。
    注意：境外住宅 IP 只是必要条件——2026-08 实测干净 HK 家宽 IP 仍可能被
    4030 风控（whaleguard 还看请求指纹），最终以 intl_verify 能否出价格为准。
    """
    import curl_cffi.requests as cffi_requests
    try:
        r = cffi_requests.get(
            IP_API.format(ip), timeout=12, impersonate="chrome"
        )
        info = r.json()
        if info.get("status") != "success":
            return {"ip": ip, "lookup": "fail", "residential": None,
                    "note": f"ip-api: {info.get('message', 'unknown')}"}
        country = (info.get("countryCode") or "").upper()
        org = (info.get("org") or "") + " " + (info.get("isp") or "")
        is_proxy = bool(info.get("proxy"))
        is_hosting = bool(info.get("hosting"))
        dc = is_hosting or is_proxy or any(k in org.lower() for k in _DC_KEYWORDS)
        residential = (country != "CN") and (not dc)
        note = []
        if not country or country == "CN":
            note.append("非境外")
        if is_proxy:
            note.append("被标记为代理/VPN 出口")
        if is_hosting:
            note.append("被标记为机房/托管")
        if dc and not is_proxy and not is_hosting:
            note.append("疑似机房/数据中心")
        if residential:
            note.append("境外住宅 IP（仅必要条件，仍可能被 4030）")
        return {
            "ip": ip,
            "lookup": "ok",
            "country": country,
            "region": info.get("regionName") or "",
            "city": info.get("city") or "",
            "isp": info.get("isp") or "",
            "org": info.get("org") or "",
            "residential": residential,
            "note": "；".join(note) or "境外住宅，条件符合",
        }
    except Exception as e:
        return {"ip": ip, "lookup": "err", "residential": None, "note": str(e)}


def run_verify(proxy: str, hotel_count: int = 1) -> None:
    """对单个代理跑一次国际版验证（前 hotel_count 家）。"""
    print(f"\n  --- 对该代理跑国际版验证（{hotel_count} 家）---")
    cmd = [sys.executable, str(Path(__file__).parent / "intl_verify.py"),
           "--proxy", proxy, "--max-hotels", str(hotel_count)]
    try:
        subprocess.run(cmd, timeout=300)
    except subprocess.TimeoutExpired:
        print("  ✗ 验证超时")


def main() -> int:
    p = argparse.ArgumentParser(description="国际版代理体检")
    p.add_argument("--proxy", action="append", default=None, help="单个代理 http://ip:port，可多次")
    p.add_argument("--test", action="store_true", default=False,
                   help="检查后直接对每个代理跑国际版验证")
    p.add_argument("--hotels", type=int, default=1, help="验证时抓几家（默认 1）")
    p.add_argument("--direct", action="store_true", default=False,
                   help="同时检查直连出口 IP（本机）")
    args = p.parse_args()

    from ctrip_hotel.config import load_config
    from ctrip_hotel.api_client import extract_proxy_pool

    proxies: list[str] = []
    if args.proxy:
        proxies = list(args.proxy)
    else:
        cfg = load_config()
        proxies = extract_proxy_pool(cfg)
    if args.direct and proxies:
        print("=== 直连（本机）===")
        ip = exit_ip(None)
        if ip:
            print(f"  出口 IP: {ip}")
            info = classify(ip)
            print(f"  归属: {info.get('country')} {info.get('region')} {info.get('city')} | {info.get('isp')}")
            print(f"  判定: {'✓ 境外住宅' if info.get('residential') else '✗ ' + info.get('note','')}")
        else:
            print("  ✗ 直连获取出口 IP 失败")
        print()

    if not proxies:
        print("未找到代理。请用 --proxy http://ip:port 指定，或在 config.yaml 配置 proxy / proxy_list / proxy_api_url。")
        return 1

    print(f"=== 代理体检：{len(proxies)} 个 ===")
    ok_any = False
    for i, proxy in enumerate(proxies, 1):
        print(f"\n[{i}/{len(proxies)}] proxy={proxy}")
        ip = exit_ip(proxy)
        if not ip:
            print("  ✗ 代理不可达 / 取出口 IP 失败")
            continue
        info = classify(ip)
        print(f"  出口 IP: {ip}")
        if info.get("lookup") == "ok":
            print(f"  归属: {info['country']} {info['region']} {info['city']}")
            print(f"  ISP/ORG: {info['org'][:70] or info['isp'][:70]}")
            print(f"  判定: {'✓ 境外住宅，条件符合' if info['residential'] else '✗ ' + info.get('note','')}")
        else:
            print(f"  归属查询失败: {info.get('note')}")
        if info.get("residential"):
            ok_any = True
        if args.test:
            run_verify(proxy, args.hotels)

    if not ok_any:
        print("\n提示：所有代理都不像境外住宅 IP。whaleguard 对数据中心/机场共享段 IP 一律 4030，")
        print("需要真正的境外家宽代理（如 HK/JP/SG 住宅 IP，或住宅代理服务商）。")
        return 2
    if not args.test:
        print("\n发现境外住宅 IP 代理。加 --test 可直接跑国际版验证看能否出价格。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
