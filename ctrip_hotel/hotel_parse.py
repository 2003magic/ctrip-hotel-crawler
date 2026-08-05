"""Merge hotel static info from page DOM + album/poi APIs."""
from __future__ import annotations

from typing import Any


def _clean_img(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    if "np-pic.png" in url or "placeholder" in url.lower():
        return None
    if "viewall" in url.lower():
        return None
    return url


def images_from_album(album: dict[str, Any] | None) -> tuple[list[str], int | None]:
    if not album:
        return [], None
    data = album.get("data") or album
    urls: list[str] = []
    seen: set[str] = set()
    total = None

    top = data.get("hotelTopImage") or {}
    if isinstance(top, dict):
        if top.get("total"):
            total = int(top["total"])
        for item in top.get("imgUrlList") or []:
            if not isinstance(item, dict):
                continue
            u = _clean_img(item.get("imgUrl"))
            # sometimes first is placeholder; dig diffPositionUrls
            if not u:
                for d in item.get("diffPositionUrls") or []:
                    u = _clean_img(d.get("picUrl"))
                    if u:
                        break
            if u and u not in seen:
                seen.add(u)
                urls.append(u)

    pop = data.get("hotelImagePop") or {}
    provide = (pop.get("hotelProvide") or {}) if isinstance(pop, dict) else {}
    for tab in provide.get("imgTabs") or []:
        if not isinstance(tab, dict):
            continue
        if tab.get("total"):
            t = int(tab["total"])
            total = t if total is None else max(total, t)
        for block in tab.get("imgUrlList") or []:
            subs = []
            if isinstance(block, dict):
                if block.get("subImgUrlList"):
                    subs = block.get("subImgUrlList") or []
                elif block.get("link"):
                    subs = [block]
            for img in subs:
                if not isinstance(img, dict):
                    continue
                u = _clean_img(img.get("link") or img.get("imgUrl"))
                if u and u not in seen:
                    seen.add(u)
                    urls.append(u)
                if len(urls) >= 60:
                    break
        if len(urls) >= 60:
            break

    return urls, (int(total) if total is not None else len(urls))


def nearby_from_additional(additional: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"metro": [], "airport": [], "train": [], "other": []}
    if not additional:
        return out
    data = additional.get("data") or additional
    poi = (data.get("hotelPoiInfo") or {}) if isinstance(data, dict) else {}
    for group in poi.get("aroundItemList") or []:
        if not isinstance(group, dict):
            continue
        for p in group.get("poiInfoList") or []:
            if not isinstance(p, dict):
                continue
            item = {
                "name": p.get("name"),
                "distance": p.get("sinkDistanceText") or p.get("distanceDescText"),
                "tags": p.get("tagNames") or [],
            }
            tags = " ".join(item["tags"]) + " " + (item["name"] or "")
            icon = str(p.get("icon") or "")
            if "地铁" in tags or "metro" in icon:
                out["metro"].append(item)
            elif "机场" in tags or "airport" in icon:
                out["airport"].append(item)
            elif "火车" in tags or "train" in icon:
                out["train"].append(item)
            else:
                if len(out["other"]) < 12:
                    out["other"].append(item)
    return out


def merge_hotel_full(
    *,
    base: dict[str, Any] | None,
    page_hotel: dict[str, Any] | None,
    album: dict[str, Any] | None,
    additional: dict[str, Any] | None,
) -> dict[str, Any]:
    base = dict(base or {})
    page = dict(page_hotel or {})
    album_imgs, album_total = images_from_album(album)
    api_nearby = nearby_from_additional(additional)
    page_nearby = page.get("nearby") or {}

    def pick_nearby(key: str) -> list:
        a = api_nearby.get(key) or []
        b = page_nearby.get(key) or []
        return a or b

    images = album_imgs or list(page.get("images") or [])
    hotel = {
        "hotel_id": page.get("hotel_id") or base.get("hotel_id"),
        "name": page.get("name") or base.get("name"),
        "star": page.get("star") if page.get("star") is not None else base.get("star"),
        "address": page.get("address") or base.get("address"),
        "score": page.get("score") if page.get("score") is not None else base.get("score"),
        "score_label": page.get("score_label"),
        "review_count": page.get("review_count")
        if page.get("review_count") is not None
        else base.get("comment_count"),
        "review_snippet": page.get("review_snippet"),
        "images": images,
        "image_count": album_total
        or page.get("image_count")
        or len(images),
        "features": page.get("features") or [],
        "facilities": page.get("facilities") or [],
        "introduction": page.get("introduction"),
        "nearby": {
            "metro": pick_nearby("metro"),
            "airport": pick_nearby("airport"),
            "train": pick_nearby("train"),
            "other": pick_nearby("other"),
        },
        "tips": None,
    }

    # reservation tips
    if additional:
        data = additional.get("data") or additional
        tips = (data.get("hotelReservationTips") or {}).get("tipList")
        if tips:
            hotel["tips"] = tips

    return hotel
