"""International-site (hk.trip.com) price client (API mode).

Domestic `getHotelRoomListInland` hides per-room prices behind login
(`priceStr == '?'`). Overseas `getHotelRoomListOversea` returns HKD prices, but
WhaleGuard gates the endpoint:

  - `phantom-token` = `window.signature(<exact POST body string>)`
  - Token is **single-use** and **bound to that body** (hotelId included)
  - Replaying a browser-consumed token → `htlSpiderActionErrorCode: 4030`
  - Wrong TLS/IP / headless session → HTTP 430 `whaleguard block`

Working API-mode approach (validated 2026-08):
  1. Warm up ONE browser page on hk.trip.com; **abort** the first
     `getHotelRoomListOversea` so the token is never consumed; keep the page
     alive only as a signer (`window.signature`).
  2. Sign in small chunks → HTTP that chunk immediately (tokens stay fresh).
  3. 4030 → resign+retry; 430 / dead signer → auto rewarm, then retry.

Headed Edge is far more reliable than headless for the signer session.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from curl_cffi import requests as cffi_requests

# ---------------------------------------------------------------------------
# HKD -> CNY exchange rate
# ---------------------------------------------------------------------------

_EXCHANGE_SOURCES = [
    "https://open.er-api.com/v6/latest/HKD",
    "https://api.exchangerate-api.com/v4/latest/HKD",
]

EXCHANGE_FALLBACK = 0.9  # HKD -> CNY rough rate used if live APIs fail

_HOTEL_ID_RE = re.compile(r'("hotelId"\s*:\s*)\d+')


def fetch_hkd_cny_rate(timeout: int = 10) -> float:
    """Return the HKD->CNY exchange rate (e.g. 0.91)."""
    for url in _EXCHANGE_SOURCES:
        try:
            r = cffi_requests.get(url, timeout=timeout, impersonate="chrome")
            j = r.json()
            rates = j.get("rates") or {}
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


def _payload_kind(payload: dict[str, Any] | None) -> str:
    """Classify a room-list response: ok | token | session | error | empty."""
    if not payload:
        return "error"
    err = str(payload.get("error") or "")
    low = err.lower()
    if "430" in low or "whaleguard" in low:
        return "session"
    data = payload.get("data")
    if not isinstance(data, dict):
        if err:
            return "error"
        return "empty"
    code = data.get("htlSpiderActionErrorCode")
    if code in (4030, "4030", 430, "430"):
        return "token" if str(code) == "4030" else "session"
    if data.get("saleRoomMap"):
        return "ok"
    if err:
        return "error"
    return "empty"


class IntlRoomClient:
    """Browser page kept as a signer; room prices fetched via pure HTTP.

    Fast+stable pipeline:
      - Sign in small chunks (= intl_workers), HTTP that chunk immediately
        (tokens stay fresh).
      - On 4030: resign same body and retry.
      - On 430 / dead signature: re-warmup session, then retry.
    """

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
        self._context = None
        self._page = None
        self._template_url: str = _ROOM_URL
        self._template_post: str = ""
        self._template_headers: dict[str, str] = {}
        self._lock = threading.RLock()
        self._session_fails = 0
        self._route_installed = False
        self._last_rewarm_at = 0.0
        self._rewarm_cooldown_sec = 15.0

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "IntlRoomClient":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        headed = bool(self.cfg.get("intl_headed", True))
        launch_kwargs: dict[str, Any] = {
            "headless": not headed,
            "args": ["--disable-dev-shm-usage"],
        }
        channel = (self.cfg.get("browser_channel") or "").lower()
        try:
            if channel in {"msedge", "chrome"}:
                self._browser = self._pw.chromium.launch(
                    channel=channel, **launch_kwargs
                )
            else:
                self._browser = self._pw.chromium.launch(**launch_kwargs)
        except Exception:
            self._browser = self._pw.chromium.launch(**launch_kwargs)

        self._new_context_and_page()
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

    def _new_context_and_page(self) -> None:
        assert self._browser is not None
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        context_kwargs: dict[str, Any] = {
            "locale": "zh-HK",
            "timezone_id": "Asia/Hong_Kong",
            "extra_http_headers": {"Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8"},
        }
        proxy = self._proxy_url()
        if proxy:
            context_kwargs["proxy"] = {"server": str(proxy)}
        self._context = self._browser.new_context(**context_kwargs)
        self._page = self._context.new_page()
        self._page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = window.chrome || { runtime: {} };
            """
        )
        self._route_installed = False

    def _proxy_url(self) -> str | None:
        proxy = self.cfg.get("intl_proxy") or self.cfg.get("proxy")
        if not proxy:
            return None
        s = str(proxy).strip()
        if not s:
            return None
        return s if "://" in s else f"http://{s}"

    def _proxies(self) -> dict[str, str] | None:
        proxy = self._proxy_url()
        if not proxy:
            return None
        return {"http": proxy, "https": proxy}

    def _retries(self) -> int:
        return max(int(self.cfg.get("intl_http_retries") or 2), 0)

    def _sign_batch_size(self, max_workers: int) -> int:
        raw = self.cfg.get("intl_sign_batch")
        if raw is None or raw == "":
            return max(int(max_workers), 1)
        return max(int(raw), 1)

    def _rewarm_threshold(self) -> int:
        return max(int(self.cfg.get("intl_rewarm_after") or 2), 1)

    # -- warmup / rewarm ---------------------------------------------------

    def _seed_ids(self) -> list[int | str]:
        seed = self.seed_hotel_id
        if seed is None:
            seed = int(self.cfg.get("seed_hotel_id") or 0) or 1
        seed_ids: list[int | str] = [seed]
        extras = self.cfg.get("seed_hotel_ids") or []
        seed_ids.extend(int(x) for x in extras if str(x).isdigit())
        return seed_ids

    def _warmup(self) -> None:
        """Capture an unused Oversea request template; keep page for signing."""
        assert self._page is not None
        seed_ids = self._seed_ids()
        captured: dict[str, Any] = {}

        def handle_route(route) -> None:
            req = route.request
            if "getHotelRoomListOversea" in req.url and "url" not in captured:
                captured["url"] = req.url
                captured["post"] = req.post_data or ""
                captured["headers"] = dict(req.headers)
                route.abort()
                return
            route.continue_()

        if self._route_installed:
            try:
                self._page.unroute("**/*getHotelRoomListOversea*")
            except Exception:
                pass
        self._page.route("**/*getHotelRoomListOversea*", handle_route)
        self._route_installed = True

        for i, sid in enumerate(seed_ids):
            url = (
                f"https://hk.trip.com/hotels/detail/"
                f"?cityId={self.cfg.get('city_id', 1)}"
                f"&hotelId={sid}"
                f"&checkIn={self.cfg['check_in']}&checkOut={self.cfg['check_out']}"
            )
            print(f"  [intl] warmup attempt {i + 1}/{len(seed_ids)}: seed={sid}")
            captured.clear()
            try:
                self._page.goto(url, timeout=60000)
            except Exception as e:
                print(f"  [warmup] goto warning: {e}")
            deadline = time.time() + 25
            while time.time() < deadline:
                if captured.get("post"):
                    break
                self._page.wait_for_timeout(400)
                if time.time() < deadline:
                    try:
                        self._page.mouse.wheel(0, 1500)
                    except Exception:
                        pass
            if captured.get("post"):
                break

        if not captured.get("post"):
            raise RuntimeError(
                "intl warmup failed: 未捕获到 getHotelRoomListOversea 请求模板。"
                "常见原因：当前网络/IP 被国际站风控（whaleguard）。"
                "请配置境外代理（proxy / intl_proxy），优先 intl_headed=true，"
                "或换 seed_hotel_id。"
            )

        self._template_url = captured["url"]
        if self._template_url.startswith("//"):
            self._template_url = "https:" + self._template_url
        self._template_post = captured["post"]
        self._template_headers = captured["headers"] or {}

        ok = False
        for _ in range(20):
            ok = bool(
                self._page.evaluate("() => typeof window.signature === 'function'")
            )
            if ok:
                break
            self._page.wait_for_timeout(200)
        if not ok:
            raise RuntimeError(
                "intl warmup failed: 页面未暴露 window.signature，无法签发 phantom-token。"
            )
        self._session_fails = 0
        print("  [intl] warmup ok: oversea template captured (signer ready)")

    def _rewarm(self, *, force: bool = False) -> bool:
        """Refresh signer session. Rate-limited to avoid WhaleGuard storms."""
        with self._lock:
            now = time.time()
            if (
                not force
                and (now - self._last_rewarm_at) < self._rewarm_cooldown_sec
            ):
                return False
            print("  [intl] rewarm: refreshing signer session…", flush=True)
            self._new_context_and_page()
            self._warmup()
            self._last_rewarm_at = time.time()
            return True

    def _maybe_rewarm(self, kind: str) -> None:
        if kind == "ok":
            self._session_fails = 0
            return
        if kind != "session":
            return
        self._session_fails += 1
        if self._session_fails >= self._rewarm_threshold():
            try:
                if self._rewarm():
                    self._session_fails = 0
            except Exception as e:
                print(f"  [intl] rewarm failed: {e}", flush=True)

    # -- signing / headers -------------------------------------------------

    def _body_for_hotel(self, hotel_id: int | str) -> str:
        """Rewrite search.hotelId in the exact captured POST string."""
        body, n = _HOTEL_ID_RE.subn(
            lambda m: m.group(1) + str(int(hotel_id)),
            self._template_post,
            count=1,
        )
        if n != 1:
            try:
                obj = json.loads(self._template_post)
                obj.setdefault("search", {})["hotelId"] = int(hotel_id)
                return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
            except Exception:
                return self._template_post
        return body

    def _sign(self, body_str: str) -> str:
        assert self._page is not None
        with self._lock:
            try:
                token = self._page.evaluate("(s) => window.signature(s)", body_str)
            except Exception as e:
                raise RuntimeError(f"signature() call failed: {e}") from e
        if not isinstance(token, str) or not token.startswith("100"):
            raise RuntimeError(f"signature() returned invalid token: {token!r}")
        return token

    def _sign_safe(self, body_str: str) -> str:
        """Sign; on failure rewarm once (rate-limited) and retry."""
        try:
            return self._sign(body_str)
        except Exception:
            # Respect cooldown — force=True caused rewarm storms under 430.
            self._rewarm(force=False)
            return self._sign(body_str)

    def _cookies(self) -> str:
        assert self._context is not None
        try:
            return "; ".join(
                f"{c['name']}={c['value']}" for c in self._context.cookies()
            )
        except Exception:
            return ""

    def _base_headers(self) -> dict[str, str]:
        skip = {
            "content-length",
            "host",
            "connection",
            "accept-encoding",
            "phantom-token",
            "cookie",
        }
        base: dict[str, str] = {}
        for k, v in (self._template_headers or {}).items():
            if k.lower() in skip or v is None:
                continue
            base[k] = v
        if not any(k.lower() == "origin" for k in base):
            base["origin"] = "https://hk.trip.com"
        if not any(k.lower() == "content-type" for k in base):
            base["content-type"] = "application/json"
        return base

    def _headers(self, token: str, cookie: str | None = None) -> dict[str, str]:
        base = self._base_headers()
        base["cookie"] = cookie if cookie is not None else self._cookies()
        base["phantom-token"] = token
        return base

    # -- price fetch (pure HTTP) -------------------------------------------

    def _http_post(self, body_str: str, token: str, cookie: str | None = None) -> dict[str, Any]:
        try:
            kwargs: dict[str, Any] = {
                "data": body_str.encode("utf-8"),
                "headers": self._headers(token, cookie=cookie),
                "impersonate": "chrome",
                "timeout": 30,
            }
            proxies = self._proxies()
            if proxies:
                kwargs["proxies"] = proxies
            r = cffi_requests.post(self._template_url, **kwargs)
            text = r.text or ""
            if not text.startswith("{"):
                return {
                    "error": f"http {r.status_code}: {text[:120]}",
                    "data": {},
                }
            j = json.loads(text)
        except Exception as e:
            return {"error": str(e), "data": {}}
        if not isinstance(j, dict):
            return {"error": "non-json response", "data": {}}
        return j

    def fetch_room(self, hotel_id: int | str) -> dict[str, Any]:
        """Sign + HTTP with resign/rewarm retries."""
        if not self._template_post:
            return {"error": "client not warmed up", "data": {}}
        body_str = self._body_for_hotel(hotel_id)
        last: dict[str, Any] = {"error": "no attempt", "data": {}}
        for attempt in range(self._retries() + 1):
            try:
                token = self._sign_safe(body_str)
            except Exception as e:
                last = {"error": str(e), "data": {}}
                continue
            last = self._http_post(body_str, token)
            kind = _payload_kind(last)
            if kind == "ok":
                self._session_fails = 0
                return last
            self._maybe_rewarm(kind)
            if attempt < self._retries():
                time.sleep(0.15 * (attempt + 1))
        return last

    def _sign_http_wave(
        self,
        hotel_ids: list[int | str],
        *,
        workers: int,
    ) -> dict[int | str, dict[str, Any]]:
        """Sign a small wave then HTTP-fetch in parallel (fresh cookies)."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        signed: list[tuple[int | str, str, str]] = []
        out: dict[int | str, dict[str, Any]] = {}
        for hid in hotel_ids:
            body_str = self._body_for_hotel(hid)
            try:
                token = self._sign_safe(body_str)
                signed.append((hid, body_str, token))
            except Exception as e:
                out[hid] = {"error": str(e), "data": {}}

        if not signed:
            return out

        cookie = self._cookies()

        def _one(job: tuple[int | str, str, str]) -> tuple[int | str, dict[str, Any]]:
            hid, body_str, token = job
            return hid, self._http_post(body_str, token, cookie=cookie)

        with ThreadPoolExecutor(max_workers=min(workers, len(signed))) as ex:
            futs = [ex.submit(_one, job) for job in signed]
            for fut in as_completed(futs):
                hid, payload = fut.result()
                out[hid] = payload
        return out

    def fetch_room_batch(
        self,
        hotel_ids: list[int | str],
        *,
        max_workers: int = 8,
    ) -> list[dict[str, Any]]:
        """Chunked sign→HTTP: short-lived tokens, limited rewarm, one retry wave."""
        if not hotel_ids:
            return []
        if not self._template_post:
            return [{"error": "client not warmed up", "data": {}} for _ in hotel_ids]

        workers = max(int(max_workers), 1)
        chunk_size = self._sign_batch_size(workers)
        results: dict[int | str, dict[str, Any]] = {}

        for i in range(0, len(hotel_ids), chunk_size):
            chunk = list(hotel_ids[i : i + chunk_size])
            wave = self._sign_http_wave(chunk, workers=workers)

            retry_ids: list[int | str] = []
            session_hits = 0
            for hid in chunk:
                payload = wave.get(hid) or {"error": "missing", "data": {}}
                kind = _payload_kind(payload)
                if kind == "ok":
                    results[hid] = payload
                    self._session_fails = 0
                else:
                    if kind == "session":
                        session_hits += 1
                    retry_ids.append(hid)

            if retry_ids and self._retries() > 0:
                # Only rewarm when the whole wave was session-blocked.
                if session_hits and session_hits >= len(chunk):
                    try:
                        self._rewarm()
                    except Exception as e:
                        print(f"  [intl] rewarm failed: {e}", flush=True)
                    time.sleep(0.8)
                else:
                    time.sleep(0.2)
                wave2 = self._sign_http_wave(retry_ids, workers=workers)
                for hid in retry_ids:
                    payload = wave2.get(hid) or wave.get(hid) or {
                        "error": "retry missing",
                        "data": {},
                    }
                    results[hid] = payload
                    if _payload_kind(payload) == "ok":
                        self._session_fails = 0
            else:
                for hid in retry_ids:
                    results[hid] = wave.get(hid) or {"error": "failed", "data": {}}

            # Pace chunks — bulk 430 often means we were too hot.
            time.sleep(1.0)

        return [
            results.get(hid) or {"error": "missing result", "data": {}}
            for hid in hotel_ids
        ]


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
    """Parse getHotelRoomListOversea into physical-room → plan prices."""
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
                    "meal": _meal_from_sale(s),
                    "cancel": _cancel_from_sale(s),
                    "confirm": _confirm_from_tags(s),
                    "occupancy": _occupancy_from_sale(s),
                    "left": _left_from_tags(s),
                    "folded": bool(s.get("isFoldStatus")),
                    "display_price": _display_price(s),
                }
            )
        prices = [p["price_hkd"] for p in plans if p["price_hkd"] is not None]
        start_hkd = min(prices) if prices else None
        out["rooms"].append(
            {
                "physical_room_id": pid,
                "room_name": p_name or (plans[0]["room_name"] if plans else None),
                "start_price_hkd": start_hkd,
                "start_price_cny": (
                    round(start_hkd * rate, 2) if start_hkd is not None else None
                ),
                "plans": plans,
            }
        )
    return out


def _parse_price(s: dict[str, Any]) -> float | None:
    """Extract numeric HKD price from a saleRoom (prefers priceInfo)."""
    pi = s.get("priceInfo")
    if isinstance(pi, dict):
        disp = str(pi.get("displayPrice") or "")
        if disp and ("?" in disp or disp.strip() in {"", "HK$", "¥"}):
            return None
        for k in ("price", "deletePricewithOutCurrency"):
            v = pi.get(k)
            if v is None:
                continue
            try:
                n = float(v)
            except (TypeError, ValueError):
                continue
            if n > 0:
                return n
        if disp:
            n = _digits_money(disp)
            if n is not None and n > 0:
                return n
    for k in ("priceStr", "showPrice", "showPriceStr", "price", "avgPrice"):
        v = s.get(k)
        if v is None:
            continue
        t = str(v).strip()
        if not t or t in {"?", "0"} or "?" in t:
            continue
        try:
            n = float(t)
            if n > 0:
                return n
        except ValueError:
            n = _digits_money(t)
            if n is not None and n > 0:
                return n
    return None


def _display_price(s: dict[str, Any]) -> str | None:
    pi = s.get("priceInfo")
    if isinstance(pi, dict) and pi.get("displayPrice"):
        return str(pi["displayPrice"])
    return None


def _digits_money(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _summary_from_tags(s: dict[str, Any]) -> str | None:
    tags = _all_tags(s)
    skip = ("人入住", "早餐", "取消", "确认", "仅剩", "开票", "含")
    summary_parts = [t for t in tags if not any(k in t for k in skip)]
    return "；".join(summary_parts) if summary_parts else None


def _all_tags(s: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for tl in (s.get("tagInfoList") or []) + (s.get("serviceTagList") or []):
        if isinstance(tl, dict) and tl.get("tagTitle"):
            tags.append(str(tl["tagTitle"]))
    return tags


def _meal_from_sale(s: dict[str, Any]) -> str | None:
    meal = s.get("mealInfo")
    if isinstance(meal, dict):
        for k in ("title", "desc", "mealType"):
            if meal.get(k):
                return str(meal[k])
    for t in _all_tags(s):
        if "早餐" in t or "含早" in t or "无早" in t:
            return t
    return None


def _cancel_from_sale(s: dict[str, Any]) -> str | None:
    cancel = s.get("cancelInfo")
    if isinstance(cancel, dict):
        for k in ("title", "desc", "cancelType"):
            if cancel.get(k):
                return str(cancel[k])
    for t in _all_tags(s):
        if "取消" in t or "不可退" in t:
            return t
    return None


def _confirm_from_tags(s: dict[str, Any]) -> str | None:
    for t in _all_tags(s):
        if "确认" in t or "立即" in t:
            return t
    return None


def _occupancy_from_sale(s: dict[str, Any]) -> int | None:
    g = s.get("guestCountInfo")
    if isinstance(g, dict):
        for k in ("adult", "guestCount", "maxGuest"):
            if g.get(k) is not None:
                try:
                    return int(g[k])
                except (TypeError, ValueError):
                    pass
    for t in _all_tags(s):
        if "人入住" in t or "成人" in t:
            m = re.search(r"(\d+)\s*人", t)
            if m:
                return int(m.group(1))
    return None


def _left_from_tags(s: dict[str, Any]) -> int | None:
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
    """Attach international price info to a hotel document."""
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
