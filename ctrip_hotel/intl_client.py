"""International-site (hk.trip.com) price client.

Ctrip's domestic `getHotelRoomListInland` hides per-room prices behind login
(`saleRoomMap.*.priceStr == '?'`). The international site (hk.trip.com) returns
the SAME data structure through `getHotelRoomListOversea` but WITHOUT the login
gate — prices come back in the requested currency (HKD).

Mechanism is identical to the domestic API mode:
  1. Warm up ONE headless browser page at hk.trip.com hotel detail; the page JS
     fires `getHotelRoomListOversea` and we capture the full request template
     (URL + POST body + phantom-token + x-ctx-* headers + cookies).
  2. Then replay with curl_cffi pure HTTP, changing only search.hotelId.

The response has the same `physicRoomMap` / `saleRoomMap` shape as the domestic
endpoint, so existing normalize logic (`build_rooms_from_api`) can parse it.
"""

from __future__ import annotations

import json
import time
from typing import Any

from curl_cffi import requests as cffi_requests

# ---------------------------------------------------------------------------
# HKD -> CNY exchange rate
# ---------------------------------------------------------------------------

# Try several public no-key APIs; fall back to 0.9 as a last resort.
_EXCHANGE_SOURCES = [
    "https://open.er-api.com/v6/latest/HKD",
    "https://api.exchangerate-api.com/v4/latest/HKD",
]

EXCHANGE_FALLBACK = 0.9  # HKD -> CNY rough rate used if live APIs fail


def fetch_hkd_cny_rate(timeout: int = 10) -> float:
    """Return the HKD->CNY exchange rate (e.g. 0.91).

    Tries live sources, caches nothing; on failure returns the fallback.
    """
    for url in _EXCHANGE_SOURCES:
        try:
            r = cffi_requests.get(url, timeout=timeout, impersonate="chrome")
            j = r.json()
            rates = (j.get("rates") or {})
            cny = rates.get("CNY")
            if cny:
                return float(cny)
        except Exception:
            continue
    return EXCHANGE_FALLBACK


# ---------------------------------------------------------------------------
# International room-list endpoint
# ---------------------------------------------------------------------------

_ROOM_URL = "https://hk.trip.com/restapi/soa2/33269/getHotelRoomListOversea"

_CAPTURE_SCRIPT = """
() => {
  const tpl = window.__CTRIP_OVERSEA_TEMPLATE__;
  if (!tpl) return null;
  return { url: tpl.url, post: tpl.post, headers: tpl.headers, hasTemplate: true };
}
"""

_INIT_SCRIPT = """
window.__CTRIP_OVERSEA_CAPTURED__ = window.__CTRIP_OVERSEA_CAPTURED__ || {};
const origFetch = window.fetch;
window.fetch = function(...args) {
  try {
    const [url, opts] = args;
    const u = String(url);
    if (u.includes('getHotelRoomListOversea')) {
      window.__CTRIP_OVERSEA_CAPTURED__.room = {
        url: u,
        post: opts ? String(opts.body || '') : '',
        headers: opts && opts.headers ? JSON.parse(JSON.stringify(opts.headers)) : {}
      };
    }
  } catch(e) {}
  return origFetch.apply(this, args);
};
"""


class IntlRoomClient:
    """One headless browser page on hk.trip.com that produces valid overseas
    phantom-tokens; afterwards room prices are fetched with pure HTTP."""

    def __init__(
        self,
        cfg: dict[str, Any],
        *,
        worker_id: int | None = None,
        seed_hotel_id: int | str | None = None,
    ) -> None:
        self.cfg = cfg
        self.worker_id = worker_id
        self.seed_hotel_id = seed_hotel_id
        self._pw = None
        self._browser = None
        self._page = None
        self._template: dict[str, Any] | None = None
        self._cookies: str = ""

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "IntlRoomClient":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        headed = bool(self.cfg.get("intl_headed", self.cfg.get("api_headed", False)))
        launch_kwargs: dict[str, Any] = {
            "headless": not headed,
            "args": ["--disable-dev-shm-usage"],
        }
        channel = (self.cfg.get("browser_channel") or "").lower()
        try:
            if channel in {"msedge", "chrome"}:
                self._browser = self._pw.chromium.launch(channel=channel, **launch_kwargs)
            else:
                self._browser = self._pw.chromium.launch(**launch_kwargs)
        except Exception:
            self._browser = self._pw.chromium.launch(**launch_kwargs)

        context_kwargs: dict[str, Any] = {
            "locale": "zh-HK",
            "timezone_id": "Asia/Hong_Kong",
            "extra_http_headers": {"Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8"},
        }
        proxy = self.cfg.get("proxy") or self.cfg.get("intl_proxy")
        if proxy:
            context_kwargs["proxy"] = {"server": str(proxy)}
        ctx = self._browser.new_context(**context_kwargs)
        self._page = ctx.new_page()
        self._page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = window.chrome || { runtime: {} };
            """
        )
        self._warmup()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # -- warmup ------------------------------------------------------------

    def _warmup(self) -> None:
        assert self._page is not None
        seed = self.seed_hotel_id
        if seed is None:
            seed = int(self.cfg.get("seed_hotel_id") or 0) or 1
        seed_ids = [seed]
        extras = self.cfg.get("seed_hotel_ids") or []
        seed_ids.extend(int(x) for x in extras if str(x).isdigit())

        self._page.add_init_script(_INIT_SCRIPT)
        captured: dict[str, Any] = {}

        for i, sid in enumerate(seed_ids):
            url = (
                f"https://hk.trip.com/hotels/detail/"
                f"?cityId={self.cfg.get('city_id', 1)}"
                f"&hotelId={sid}"
                f"&checkIn={self.cfg['check_in']}&checkOut={self.cfg['check_out']}"
            )
            print(f"  [intl] warmup attempt {i + 1}/{len(seed_ids)}: seed={sid}")
            self._page.evaluate("() => { window.__CTRIP_OVERSEA_CAPTURED__ = {}; }")
            try:
                self._page.goto(url, timeout=60000)
            except Exception as e:
                print(f"  [warmup] goto warning: {e}")
            deadline = time.time() + 20
            while time.time() < deadline:
                cap = self._page.evaluate(
                    "() => window.__CTRIP_OVERSEA_CAPTURED__ || {}"
                )
                if cap.get("room"):
                    captured["room"] = cap["room"]
                    break
                self._page.wait_for_timeout(500)
                if time.time() < deadline:
                    try:
                        self._page.mouse.wheel(0, 1500)
                    except Exception:
                        pass
            if "room" in captured:
                break

        if "room" not in captured:
            raise RuntimeError(
                "intl warmup failed: 未捕获到 getHotelRoomListOversea 请求模板。"
                "常见原因：当前网络/IP 被国际站风控（whaleguard block / 4030）。"
                "可配置代理（config.yaml 的 proxy / intl_proxy），或稍后重试，"
                "或换 seed_hotel_id。"
            )
        self._page.evaluate(
            """(tpl) => { window.__CTRIP_OVERSEA_TEMPLATE__ = tpl; }""",
            {"url": captured["room"]["url"], "post": captured["room"]["post"]},
        )
        self._template = captured["room"]
        try:
            self._cookies = "; ".join(
                f"{c['name']}={c['value']}" for c in self._page.context.cookies()
            )
        except Exception:
            self._cookies = ""
        print("  [intl] warmup ok: oversea template captured")

    # -- price fetch (pure HTTP, token reuse) ------------------------------

    def _headers(self) -> dict[str, str]:
        hdrs = self._template.get("headers") or {}
        base = {
            "User-Agent": hdrs.get("user-agent")
            or "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": hdrs.get("accept-language") or "zh-HK,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "cookieOrigin": hdrs.get("cookieorigin") or "https://hk.trip.com",
            "Origin": hdrs.get("origin") or "https://hk.trip.com",
            "Referer": hdrs.get("referer") or "https://hk.trip.com/hotels/detail/",
            "locale": hdrs.get("locale") or "zh-HK",
            "currency": hdrs.get("currency") or "HKD",
            "phantom-token": hdrs.get("phantom-token") or "",
            "cookie": self._cookies or "",
        }
        # carry the x-ctx-* headers the page sets
        for k in (
            "x-ctx-currency",
            "x-ctx-locale",
            "x-ctx-country",
            "x-ctx-wclient-req",
            "x-ctx-ubt-pageid",
            "x-ctx-ubt-vid",
            "x-ctx-ubt-sid",
            "x-ctx-ubt-pvid",
            "w-payload-source",
        ):
            v = hdrs.get(k)
            if v:
                base[k] = v
        return base

    def fetch_room(self, hotel_id: int | str) -> dict[str, Any]:
        """Fetch room status + prices for one hotel via pure HTTP."""
        assert self._template is not None, "client not warmed up"
        try:
            body = json.loads(self._template["post"])
            body["search"]["hotelId"] = int(hotel_id)
        except Exception:
            body = {"search": {"hotelId": int(hotel_id)}}
        url = self._template["url"]
        if url.startswith("//"):
            url = "https:" + url
        try:
            r = cffi_requests.post(
                url,
                json=body,
                headers=self._headers(),
                impersonate="chrome",
                timeout=25,
            )
            j = r.json()
        except Exception as e:
            return {"error": str(e), "data": {}}
        if not isinstance(j, dict):
            return {"error": "non-json response", "data": {}}
        return j

    def fetch_room_batch(
        self,
        hotel_ids: list[int | str],
        *,
        max_workers: int = 8,
    ) -> list[dict[str, Any]]:
        if max_workers <= 1:
            out = []
            for hid in hotel_ids:
                try:
                    out.append(self.fetch_room(hid))
                except Exception as e:
                    out.append({"error": str(e), "data": {}})
                time.sleep(float(self.cfg.get("delay_ms") or 0) / 1000.0)
            return out
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: dict[int | str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(self.fetch_room, hid): hid for hid in hotel_ids}
            for fut in as_completed(futs):
                hid = futs[fut]
                try:
                    results[hid] = fut.result()
                except Exception as e:
                    results[hid] = {"error": str(e), "data": {}}
        return [results[hid] for hid in hotel_ids]


# ---------------------------------------------------------------------------
# Parsing / merging
# ---------------------------------------------------------------------------


def normalize_intl_prices(
    payload: dict[str, Any] | None,
    *,
    check_in: str,
    check_out: str,
    rate: float,
) -> dict[str, Any]:
    """Parse the getHotelRoomListOversea response into a price list.

    Returns:
    {
      currency: "HKD",
      exchange_rate: 0.91,
      exchange_currency: "CNY",
      rooms: [
        {
          physical_room_id: "...",
          room_name: "...",
          start_price_hkd: 328,       # min over plans, if available
          start_price_cny: 298,
          plans: [
            {
              plan_id, room_name, price_hkd, price_cny,
              summary, meal, cancel, confirm, occupancy, left,
              folded: bool,           # isFoldStatus == 1
            },
            ...
          ]
        },
        ...
      ]
    }
    """
    out: dict[str, Any] = {
        "currency": "HKD",
        "exchange_rate": rate,
        "exchange_currency": "CNY",
        "rooms": [],
    }
    if not payload:
        return out
    data = payload.get("data") or {}
    if data.get("htlSpiderActionErrorCode"):
        return out
    physic = data.get("physicRoomMap") or {}
    sale = data.get("saleRoomMap") or {}
    if not isinstance(sale, dict):
        return out

    # group sales by physicalRoomId
    by_physic: dict[str, list[dict[str, Any]]] = {}
    for s in sale.values():
        if not isinstance(s, dict):
            continue
        phys_id = s.get("physicalRoomId")
        by_physic.setdefault(str(phys_id), []).append(s)

    for pid, sales in by_physic.items():
        p_name = None
        if isinstance(physic, dict) and pid in physic:
            p_name = (physic[pid] or {}).get("name")
        plans = []
        for s in sales:
            price = _parse_price(s)
            plans.append(
                {
                    "plan_id": s.get("id"),
                    "room_name": s.get("name") or p_name,
                    "price_hkd": price,
                    "price_cny": round(price * rate, 2) if price is not None else None,
                    "summary": _summary_from_tags(s),
                    "meal": _meal_from_tags(s),
                    "cancel": _cancel_from_tags(s),
                    "confirm": _confirm_from_tags(s),
                    "occupancy": _occupancy_from_tags(s),
                    "left": _left_from_tags(s),
                    "folded": bool(s.get("isFoldStatus")),
                }
            )
        prices = [p["price_hkd"] for p in plans if p["price_hkd"] is not None]
        start_hkd = min(prices) if prices else None
        out["rooms"].append(
            {
                "physical_room_id": pid,
                "room_name": p_name or (plans[0]["room_name"] if plans else None),
                "start_price_hkd": start_hkd,
                "start_price_cny": round(start_hkd * rate, 2) if start_hkd is not None else None,
                "plans": plans,
            }
        )
    return out


def _parse_price(s: dict[str, Any]) -> float | None:
    """Extract a numeric price from a saleRoom, trying several field names."""
    for k in ("priceStr", "showPrice", "showPriceStr", "price", "avgPrice"):
        v = s.get(k)
        if v is None:
            continue
        t = str(v).strip()
        if not t or t in {"?", "0"}:
            continue
        try:
            return float(t)
        except ValueError:
            continue
    return None


def _summary_from_tags(s: dict[str, Any]) -> str | None:
    tags = _all_tags(s)
    # The plan summary is the "房型摘要" - a combination of tag titles minus
    # generic ones (occupancy / breakfast / cancel / confirm / qty).
    SKIP = ("人入住", "早餐", "取消", "确认", "仅剩", "开票", "含")
    summary_parts = [t for t in tags if not any(k in t for k in SKIP)]
    return "；".join(summary_parts) if summary_parts else None


def _all_tags(s: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for tl in (s.get("tagInfoList") or []) + (s.get("serviceTagList") or []):
        if isinstance(tl, dict) and tl.get("tagTitle"):
            tags.append(str(tl["tagTitle"]))
    return tags


def _meal_from_tags(s: dict[str, Any]) -> str | None:
    for t in _all_tags(s):
        if "早餐" in t or "含早" in t or "无早" in t:
            return t
    return None


def _cancel_from_tags(s: dict[str, Any]) -> str | None:
    for t in _all_tags(s):
        if "取消" in t or "不可退" in t:
            return t
    return None


def _confirm_from_tags(s: dict[str, Any]) -> str | None:
    for t in _all_tags(s):
        if "确认" in t or "立即" in t:
            return t
    return None


def _occupancy_from_tags(s: dict[str, Any]) -> int | None:
    import re
    for t in _all_tags(s):
        if "人入住" in t or "成人" in t:
            m = re.search(r"(\d+)\s*人", t)
            if m:
                return int(m.group(1))
    return None


def _left_from_tags(s: dict[str, Any]) -> int | None:
    import re
    for t in _all_tags(s):
        if "仅剩" in t:
            m = re.search(r"仅剩\s*(\d+)\s*间", t)
            if m:
                return int(m.group(1))
    return None


def merge_prices_into_doc(
    doc: dict[str, Any],
    price_info: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach international price info to a hotel document.

    - `doc["hotel"]["price_info"]` gets the full normalized price structure.
    - Each `doc["rooms"][*]` gets a matching `prices` list (plans with prices)
      keyed by physical_room_id so the preview can show per-room prices.
    """
    doc = dict(doc)
    hotel = dict(doc.get("hotel") or {})
    if price_info:
        hotel["price_info"] = price_info
        hotel["min_price_hkd"] = _min_overall(price_info)
        if hotel.get("min_price_hkd") is not None:
            hotel["min_price_cny"] = round(
                hotel["min_price_hkd"] * price_info.get("exchange_rate", 0.9), 2
            )
    doc["hotel"] = hotel

    # map physical_room_id -> plans
    plans_by_physic: dict[str, list[dict[str, Any]]] = {}
    if price_info:
        for r in price_info.get("rooms") or []:
            plans_by_physic[str(r.get("physical_room_id"))] = r.get("plans") or []

    rooms = []
    for room in doc.get("rooms") or []:
        room = dict(room)
        rid = str(room.get("room_id"))
        if rid in plans_by_physic:
            room["prices"] = plans_by_physic[rid]
        rooms.append(room)
    doc["rooms"] = rooms
    return doc


def _min_overall(price_info: dict[str, Any]) -> float | None:
    vals = [
        r.get("start_price_hkd")
        for r in price_info.get("rooms") or []
        if r.get("start_price_hkd") is not None
    ]
    return min(vals) if vals else None
