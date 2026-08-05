from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, Response, sync_playwright

from ctrip_hotel.config import ROOT

# Live SOA2 markers — capture if the page fires them; not required for crawl.
LIST_URL_MARKERS = ("fetchHotelList",)
ROOM_URL_MARKERS = ("getHotelRoomListInland", "getHotelRoomList")
ALBUM_URL_MARKERS = ("ctgethotelalbum", "ctGetHotelAlbum")
ADDITIONAL_URL_MARKERS = ("getDetailAdditionalInfo",)

STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
"""

# Hotel detail page — parse visible blocks (name/address/features/facilities/intro/reviews/nearby).
PARSE_HOTEL_JS = """() => {
  const text = (el) => (el && (el.innerText || el.textContent) || '').replace(/\\s+/g, ' ').trim();
  const body = text(document.body);
  const name =
    text(document.querySelector('h1')) ||
    ((document.title || '').match(/^([^预订_\\[]+)/) || [])[1] ||
    null;
  // title often: 酒店名_城市酒店预订...
  if (!name) {
    const tm = (document.title || '').split(/[_|]/);
    if (tm[0] && tm[0].trim()) name = tm[0].trim();
  }

  let address = null;
  if (name) {
    const m = body.match(new RegExp(
      name.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&') + '\\\\s+([^选择房间]{6,60}?)\\\\s*显示地图'
    ));
    if (m) address = m[1].trim();
  }
  if (!address) {
    const m2 = body.match(/((?:北京|上海|广州|深圳|成都|杭州|重庆|天津|南京|武汉|西安|苏州|青岛|长沙|郑州|宁波|佛山|东莞|无锡|合肥|昆明|沈阳|大连|厦门|福州|济南|哈尔滨|长春)[\\u4e00-\\u9fff\\d]{2,40}?(?:路|街|大街|道|巷)[\\u4e00-\\u9fff\\d号]{1,20})/);
    if (m2) address = m2[1];
  }
  if (address && name && address.includes(name)) {
    address = address.replace(name, '').trim();
  }

  const sliceBetween = (a, b) => {
    const i = body.indexOf(a);
    if (i < 0) return '';
    const rest = body.slice(i + a.length);
    const j = b ? rest.search(b) : -1;
    return (j >= 0 ? rest.slice(0, j) : rest).trim();
  };

  const featRaw = sliceBetween('酒店特色', /酒店设施|酒店简介/);
  const features = featRaw
    .replace(/\\+\\d+项/g, ' ')
    .split(/\\s+/)
    .map(s => s.trim())
    .filter(s => s && s.length >= 2 && s.length <= 12 && !/酒店|查看|选择/.test(s))
    .slice(0, 12)
    .map(n => ({ name: n }));

  const facRaw = sliceBetween('酒店设施', /所有设施|酒店简介|查看更多/);
  const facilities = [];
  const facSeen = new Set();
  const pushFac = (raw) => {
    let name = (raw || '').trim();
    if (!name || name.length < 2 || name.length > 28) return;
    if (/酒店|查看|选择|点评|房间|关闭|设施服务|热门|更多/.test(name)) return;
    // drop tiny english fragments from restaurant brands
    if (/^[A-Za-z]{1,3}$/.test(name)) return;
    let tag = null;
    if (name.includes('免费')) { tag = '免费'; name = name.replace(/免费/g, '').trim(); }
    if (!name || facSeen.has(name)) return;
    facSeen.add(name);
    facilities.push({ name, tag });
  };
  // prefer multi-char chinese / brand phrases
  for (const m of facRaw.matchAll(/[\\u4e00-\\u9fa5A-Za-z0-9·&']{2,18}/g)) pushFac(m[0]);
  // facility modal / drawer if opened
  for (const dlg of document.querySelectorAll('[role="dialog"], .ant-modal, [class*="drawer"], [class*="popup"]')) {
    const dt = text(dlg);
    if (!dt || dt.length < 10) continue;
    if (!(dt.includes('设施') || dt.includes('服务') || dt.includes('免费'))) continue;
    for (const el of dlg.querySelectorAll('div, span, li')) {
      const t = text(el);
      if (t && t.length <= 16) pushFac(t);
      if (facilities.length >= 40) break;
    }
  }

  let introduction = sliceBetween('酒店简介', /查看更多|\\d\\.\\d\\s*(超棒|很好|不错)/);
  if (introduction.length > 800) introduction = introduction.slice(0, 800);
  if (introduction.length < 20) introduction = null;

  const scoreMatch = body.match(/(\\d\\.\\d)\\s*(超棒|很好|不错|棒|好评)/);
  const reviewMatch = body.match(/显示所有\\s*([\\d,]+)\\s*条点评/) || body.match(/([\\d,]+)\\s*条点评/);
  let review_snippet = null;
  if (scoreMatch) {
    const idx = body.indexOf(scoreMatch[0]);
    const after = body.slice(idx + scoreMatch[0].length, idx + scoreMatch[0].length + 180);
    const sn = after.split(/显示所有|附近|房间/)[0].trim();
    if (sn.length >= 10) review_snippet = sn;
  }

  const nearby = { metro: [], airport: [], train: [], other: [] };
  const nearChunk = sliceBetween('附近', /在地图上查看|房间\\s|点评\\s|服务及设施/) || (body.match(/附近([\\s\\S]{0,500})/) || [])[1] || '';
  for (const m of nearChunk.matchAll(/(地铁|机场|火车站)\\s*[:：]?\\s*([^（(]{2,30})\\s*[（(]\\s*([^）)]+)\\s*[）)]/g)) {
    const item = { name: m[2].trim(), distance: m[3].trim(), tags: [m[1]] };
    if (m[1] === '地铁') nearby.metro.push(item);
    else if (m[1] === '机场') nearby.airport.push(item);
    else if (m[1] === '火车站') nearby.train.push(item);
  }

  const ic = body.match(/查看所有\\s*(\\d+)\\s*张照片/) || body.match(/(\\d+)\\s*张照片/);
  // diamond / star count from aria if present
  let star = null;
  const aria = document.querySelector('[aria-label*=\"out of\"], [aria-label*=\"星\"]');
  if (aria) {
    const am = (aria.getAttribute('aria-label') || '').match(/(\\d+)/);
    if (am) star = Number(am[1]);
  }

  return {
    name: name || null,
    address,
    score: scoreMatch ? Number(scoreMatch[1]) : null,
    score_label: scoreMatch ? scoreMatch[2] : null,
    review_count: reviewMatch ? Number(String(reviewMatch[1]).replace(/,/g, '')) : null,
    review_snippet,
    images: [],
    image_count: ic ? Number(ic[1]) : null,
    features,
    facilities,
    introduction,
    nearby,
    star,
  };
}"""

# Meta / JSON-LD fallbacks for address / name / star
PARSE_HOTEL_META_JS = """() => {
  const out = {};
  const abs = (u) => { try { return new URL(u, location.href).href; } catch { return u; } };
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const j = JSON.parse(s.textContent || '{}');
      const nodes = Array.isArray(j) ? j : (j['@graph'] || [j]);
      for (const n of nodes) {
        if (!n || typeof n !== 'object') continue;
        const t = (n['@type'] || '').toString();
        if (/Hotel|LodgingBusiness/i.test(t) || n.address || n.name) {
          if (n.name) out.name = n.name;
          if (n.starRating?.ratingValue) out.star = Number(n.starRating.ratingValue);
          if (typeof n.starRating === 'number') out.star = n.starRating;
          const addr = n.address;
          if (typeof addr === 'string') out.address = addr;
          else if (addr && typeof addr === 'object') {
            out.address = [addr.streetAddress, addr.addressLocality, addr.addressRegion]
              .filter(Boolean).join('');
          }
          if (n.description && !out.introduction) out.introduction = String(n.description).slice(0, 500);
          if (Array.isArray(n.image)) {
            out.images = n.image.map(x => typeof x === 'string' ? abs(x) : abs(x?.url)).filter(Boolean);
          } else if (typeof n.image === 'string') {
            out.images = [abs(n.image)];
          }
        }
      }
    } catch (e) {}
  }
  const ogTitle = document.querySelector('meta[property="og:title"]')?.content;
  const ogDesc = document.querySelector('meta[property="og:description"]')?.content;
  const ogImg = document.querySelector('meta[property="og:image"]')?.content;
  if (ogTitle && !out.name) out.name = ogTitle.split('_')[0].split('[')[0].trim();
  if (ogDesc && !out.introduction) out.introduction = ogDesc.slice(0, 500);
  if (ogImg && !(out.images||[]).length) out.images = [abs(ogImg)];
  return out;
}"""

# Parse the hotel detail room list as a human sees it (房型 + 早餐/取消/价格或解锁优惠).
PARSE_ROOMS_JS = """() => {
  const text = (el) => (el && (el.innerText || el.textContent) || '').replace(/\\s+/g, ' ').trim();
  const BAD = /(选择房间|房间详情|今日价格|可住|房型摘要|筛选|登录|优惠|设施|点评|位置|政策|预订须知|卫浴|便利|景观|媒体|食品|儿童|无障碍|服务|网络|特色|客房|浴室)/;
  const rows = [];

  // Anchor on「房间详情」links — each sits under a real physical room card.
  const detailLinks = Array.from(document.querySelectorAll('a, span, div, button'))
    .filter(el => text(el) === '房间详情');

  const seen = new Set();
  for (const link of detailLinks.slice(0, 40)) {
    let card = link;
    for (let i = 0; i < 10 && card.parentElement; i++) {
      card = card.parentElement;
      const ct = text(card);
      if (ct.includes('房间详情') && (ct.includes('有窗') || ct.includes('无窗') || ct.includes('平方米') || /\\d+张/.test(ct))) {
        break;
      }
    }
    const cardText = text(card);
    if (!cardText.includes('房间详情')) continue;

    // room title: short heading-like node that ends with 房/床/套房
    let roomName = null;
    const candidates = Array.from(card.querySelectorAll('div, span, h2, h3, h4, a'))
      .map(el => text(el))
      .filter(t => t && t.length >= 2 && t.length <= 36
        && /(房|套房|公寓)$/.test(t)
        && !BAD.test(t)
        && !/^\\d+张/.test(t)
        && !/平方米|层$/.test(t));
    roomName = candidates[0] || null;
    if (!roomName) continue;
    if (seen.has(roomName)) continue;
    seen.add(roomName);

    const area = (cardText.match(/(\\d+(?:\\.\\d+)?\\s*-\\s*\\d+(?:\\.\\d+)?\\s*平方米)/) || [])[1] || null;
    const floor = (cardText.match(/(\\d+\\s*-\\s*\\d+\\s*层)/) || [])[1] || null;
    const bed = (cardText.match(/(\\d+张[^|\\n]{0,16}床)/) || [])[1] || null;

    // One row per physical room (policies without price)
    const meal = (cardText.match(/(\\d+份(?:\\/\\d+份)?早餐|无早餐|含早餐)/) || [])[1] || null;
    const cancel = (cardText.match(/(不可取消|免费取消|限时取消|阶梯取消)/) || [])[1] || null;
    const left = (cardText.match(/仅剩\\s*(\\d+)\\s*间/) || [])[1];

    rows.push({
      room_name: roomName,
      bed,
      window: cardText.includes('有窗') ? '有窗' : (cardText.includes('无窗') ? '无窗' : null),
      smoke: cardText.includes('禁烟') ? '禁烟' : null,
      area,
      floor,
      wifi: /wi-?fi/i.test(cardText) ? 'Wi-Fi免费' : null,
      has_room_detail_link: true,
      sales: [{
        summary: null,
        meal,
        cancel,
        confirm: cardText.includes('立即确认') ? '立即确认' : null,
        price: null,
        price_locked: false,
        left: left ? Number(left) : null,
      }],
      card_text: cardText.slice(0, 400),
    });
  }
  return rows;
}"""


class CtripHotelClient:
    """Browse like a user: list -> hotel detail -> room cards / 房间详情."""

    def __init__(self, cfg: dict[str, Any], *, worker_id: int | None = None) -> None:
        self.cfg = cfg
        self.worker_id = worker_id
        # Each worker needs its own profile — Chromium locks user-data-dir.
        # Keep main session at .browser-profile (reuse first successful login/verify).
        # Workers get isolated dirs so Chromium user-data-dir locks don't collide.
        if worker_id is None:
            self.profile_dir = ROOT / ".browser-profile"
        else:
            self.profile_dir = ROOT / ".browser-profile-workers" / f"w{worker_id}"
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._list_payloads: list[dict[str, Any]] = []
        self._room_payloads: dict[str, dict[str, Any]] = {}
        self._album_payloads: list[dict[str, Any]] = []
        self._additional_payloads: list[dict[str, Any]] = []
        self._pw = None
        self._context: BrowserContext | None = None

    def __enter__(self) -> "CtripHotelClient":
        self._pw = sync_playwright().start()
        channel = (self.cfg.get("browser_channel") or "msedge").lower()
        headed = bool(self.cfg.get("headed", True))
        kwargs: dict[str, Any] = {
            "headless": not headed,
            "locale": "zh-CN",
            "viewport": {"width": 1440, "height": 960},
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        user_data = str(self.profile_dir)
        try:
            if channel in {"msedge", "chrome"}:
                self._context = self._pw.chromium.launch_persistent_context(
                    user_data, channel=channel, **kwargs
                )
            else:
                self._context = self._pw.chromium.launch_persistent_context(
                    user_data, **kwargs
                )
        except Exception:
            self._context = self._pw.chromium.launch_persistent_context(
                user_data, **kwargs
            )
        self._context.add_init_script(STEALTH_INIT)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._context:
            self._context.close()
        if self._pw:
            self._pw.stop()

    def _on_response(self, resp: Response) -> None:
        path = urlparse(resp.url).path
        low = path.lower()
        try:
            if any(m.lower() in low for m in LIST_URL_MARKERS):
                body = resp.json()
                if isinstance(body, dict):
                    self._list_payloads.append(body)
            elif any(m.lower() in low for m in ROOM_URL_MARKERS):
                body = resp.json()
                if isinstance(body, dict):
                    self._room_payloads[f"room_{int(time.time() * 1000)}"] = body
            elif any(m.lower() in low for m in ALBUM_URL_MARKERS):
                body = resp.json()
                if isinstance(body, dict):
                    self._album_payloads.append(body)
            elif any(m.lower() in low for m in ADDITIONAL_URL_MARKERS):
                body = resp.json()
                if isinstance(body, dict):
                    self._additional_payloads.append(body)
        except Exception:
            return

    def list_url(self) -> str:
        return (
            f"https://hotels.ctrip.com/hotels/list?"
            f"city={self.cfg['city_id']}"
            f"&checkin={self.cfg['check_in']}"
            f"&checkout={self.cfg['check_out']}"
        )

    def detail_url(self, hotel_id: int | str) -> str:
        return (
            f"https://hotels.ctrip.com/hotels/{hotel_id}.html"
            f"?checkIn={self.cfg['check_in']}&checkOut={self.cfg['check_out']}"
        )

    def fetch_hotel_list(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        assert self._context is not None
        self._list_payloads.clear()
        page = self._context.new_page()
        page.on("response", self._on_response)
        page.goto(self.list_url(), wait_until="domcontentloaded", timeout=60000)

        headed = bool(self.cfg.get("headed", True))
        wait_sec = int(self.cfg.get("verify_wait_sec") or (180 if headed else 20))
        deadline = time.time() + wait_sec
        title = ""
        while time.time() < deadline:
            try:
                if page.is_closed():
                    break
                title = page.title() or title
                if self._list_payloads or page.locator("[data-offline-hotelId]").count() > 0:
                    break
                page.wait_for_timeout(1000 if headed else 500)
            except Exception:
                break

        try:
            if not page.is_closed():
                page.wait_for_timeout(1200)
                pages = int(self.cfg.get("pages") or 1)
                for _ in range(max(pages - 1, 0)):
                    page.mouse.wheel(0, 4200)
                    page.wait_for_timeout(2000)
                dom_cards = self._parse_dom_cards(page)
                title = page.title() or title
                page.close()
            else:
                dom_cards = []
        except Exception:
            dom_cards = []

        if not self._list_payloads and not dom_cards:
            raise RuntimeError(
                "未拿到酒店列表。常见原因是自动化被风控页拦截（不是必须登录账号）。"
                "请 headed: true 打开窗口，如有滑块/人机验证点一下即可；"
                f"已等待约 {wait_sec}s。标题={title or '-'}"
            )
        return list(self._list_payloads), dom_cards

    def fetch_room_status(self, hotel_id: int | str) -> dict[str, Any]:
        """Hotel page + rooms: capture album/poi/room APIs and parse visible hotel blocks."""
        assert self._context is not None
        before = set(self._room_payloads.keys())
        album_before = len(self._album_payloads)
        add_before = len(self._additional_payloads)
        page = self._context.new_page()
        page.on("response", self._on_response)
        page.goto(self.detail_url(hotel_id), wait_until="domcontentloaded", timeout=60000)

        # wait for room UI or API + hotel header
        for _ in range(45):
            has_header = False
            try:
                has_header = bool(page.locator("h1").count())
            except Exception:
                pass
            if page.get_by_text("房间详情").count() > 0 and has_header:
                break
            new_keys = [k for k in self._room_payloads if k not in before]
            if new_keys:
                data = self._room_payloads[new_keys[-1]].get("data") or {}
                if data.get("physicRoomMap") or data.get("saleRoomMap"):
                    break
            page.wait_for_timeout(400)

        # load below-the-fold intro / nearby / facilities
        for _ in range(6):
            self._safe_call(page, lambda: page.mouse.wheel(0, 1000))
            self._safe_call(page, lambda: page.wait_for_timeout(350))

        # expand intro / all facilities for fuller text
        self._expand_hotel_sections(page)

        if bool(self.cfg.get("unlock_price", False)):
            self._safe_call(page, lambda: self._click_unlock_offers(page))

        self._safe_call(page, lambda: page.wait_for_timeout(700))
        page_hotel = self._safe_call(page, lambda: page.evaluate(PARSE_HOTEL_JS)) or {}
        if isinstance(page_hotel, dict):
            page_hotel["hotel_id"] = hotel_id
            # JSON-LD / meta fallbacks
            meta = self._safe_call(page, lambda: page.evaluate(PARSE_HOTEL_META_JS)) or {}
            if isinstance(meta, dict):
                for k, v in meta.items():
                    if v and not page_hotel.get(k):
                        page_hotel[k] = v
        dom_rooms = self._safe_call(page, lambda: page.evaluate(PARSE_ROOMS_JS)) or []

        detail_extras: list[dict[str, Any]] = []
        if bool(self.cfg.get("open_room_detail", False)):
            detail_extras = (
                self._safe_call(
                    page,
                    lambda: self._open_room_details(
                        page, limit=int(self.cfg.get("room_detail_limit") or 3)
                    ),
                )
                or []
            )

        api_payload: dict[str, Any] | None = None
        new_keys = [k for k in self._room_payloads if k not in before]
        if new_keys:
            api_payload = self._room_payloads[new_keys[-1]]
        album = self._album_payloads[-1] if len(self._album_payloads) > album_before else None
        additional = (
            self._additional_payloads[-1]
            if len(self._additional_payloads) > add_before
            else None
        )

        source = (
            "api"
            if api_payload and (api_payload.get("data") or {}).get("physicRoomMap")
            else ("dom" if dom_rooms else "none")
        )

        try:
            page.close()
        except Exception:
            pass
        return {
            "source": source,
            "hotel_id": hotel_id,
            "page_hotel": page_hotel,
            "dom_rooms": dom_rooms or [],
            "room_detail_extras": detail_extras,
            "api": api_payload,
            "album": album,
            "additional": additional,
        }

    @staticmethod
    def _safe_call(page: Page, fn):
        try:
            if page.is_closed():
                return None
            return fn()
        except Exception:
            return None

    def _expand_hotel_sections(self, page: Page) -> None:
        for label in ("查看更多", "所有设施", "酒店设施"):
            loc = page.get_by_text(label, exact=False)
            n = min(loc.count(), 3)
            for i in range(n):
                try:
                    loc.nth(i).click(timeout=1200)
                    page.wait_for_timeout(800)
                except Exception:
                    continue
        # keep expanded — PARSE_HOTEL reads body / dialog text

    def _click_unlock_offers(self, page: Page) -> None:
        buttons = page.get_by_text("解锁优惠", exact=False)
        n = min(buttons.count(), 6)
        for i in range(n):
            try:
                buttons.nth(i).click(timeout=1500)
                page.wait_for_timeout(600)
            except Exception:
                continue

    def _open_room_details(self, page: Page, *, limit: int) -> list[dict[str, Any]]:
        extras: list[dict[str, Any]] = []
        links = page.get_by_text("房间详情", exact=True)
        n = min(links.count(), limit)
        for i in range(n):
            try:
                links.nth(i).click(timeout=2000)
                page.wait_for_timeout(900)
                # grab visible dialog/drawer text
                panel_text = page.evaluate(
                    """() => {
                      const dialogs = Array.from(document.querySelectorAll(
                        '[role="dialog"], .ant-modal, .drawer, [class*="modal"], [class*="drawer"], [class*="popup"]'
                      ));
                      const visible = dialogs
                        .map(el => (el.innerText || '').trim())
                        .filter(t => t && t.length > 20)
                        .sort((a,b) => b.length - a.length);
                      return visible[0] || '';
                    }"""
                )
                extras.append({"index": i, "detail_text": (panel_text or "")[:2000]})
                # close panel
                for key in ("Escape",):
                    page.keyboard.press(key)
                page.wait_for_timeout(300)
                # click close if still open
                closer = page.locator(
                    '[aria-label="Close"], .ant-modal-close, button:has-text("关闭")'
                )
                if closer.count():
                    try:
                        closer.first.click(timeout=800)
                    except Exception:
                        pass
            except Exception as e:
                extras.append({"index": i, "error": str(e)})
        return extras

    @staticmethod
    def _parse_dom_cards(page: Page) -> list[dict[str, Any]]:
        cards = page.eval_on_selector_all(
            "[data-offline-hotelId]",
            """els => els.map(el => {
                const id = el.getAttribute('data-offline-hotelId');
                const nameEl = el.querySelector('.hotelName');
                const priceEl = el.querySelector('[class*="price"], .hotel-price, .sale');
                const scoreEl = el.querySelector('[class*="score"], .score');
                const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                const addr = (t.match(/((?:[\u4e00-\u9fa5]{2,}(?:区|县|镇))?[\u4e00-\u9fa5\\d]{2,20}(?:路|街|大街|道|巷)[\u4e00-\u9fa5\\d号]{0,16})/) || [])[1] || null;
                const starAria = el.querySelector('[aria-label*="out of"], [aria-label*="星"]');
                let star = null;
                if (starAria) {
                  const m = (starAria.getAttribute('aria-label') || '').match(/(\\d+)/);
                  if (m) star = Number(m[1]);
                }
                return {
                  hotel_id: id,
                  name: nameEl ? nameEl.textContent.trim() : null,
                  min_price: priceEl ? (priceEl.textContent || '').replace(/[^0-9.]/g,'') : null,
                  score: scoreEl ? (scoreEl.textContent || '').trim() : null,
                  address: addr,
                  star,
                };
            })""",
        )
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for c in cards or []:
            hid = c.get("hotel_id")
            if not hid or hid in seen:
                continue
            seen.add(hid)
            out.append(c)
        return out


def save_raw_payloads(run_dir: Path, name: str, payloads: list[dict[str, Any]]) -> None:
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    for i, p in enumerate(payloads):
        (raw_dir / f"{name}_{i + 1}.json").write_text(
            json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8"
        )
