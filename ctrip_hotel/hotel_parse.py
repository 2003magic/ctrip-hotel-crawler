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
            tags = " ".join(str(x) for x in (item["tags"] or [])) + " " + (item["name"] or "")
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


def introduction_from_additional(additional: dict[str, Any] | None) -> str | None:
    """Build a short intro from getDetailAdditionalInfo.hotelIntroduction."""
    if not additional:
        return None
    data = additional.get("data") or additional
    intro = data.get("hotelIntroduction") or {}
    if not isinstance(intro, dict):
        return None
    parts: list[str] = []
    for card in intro.get("highLightCardList") or []:
        if not isinstance(card, dict):
            continue
        name = (card.get("tagName") or "").strip()
        desc = (card.get("tagDesc") or "").strip()
        if name and desc:
            parts.append(f"{name}：{desc}")
        elif desc:
            parts.append(desc)
        elif name:
            parts.append(name)
    for sec in intro.get("sectionList") or []:
        if not isinstance(sec, dict):
            continue
        desc = (sec.get("desc") or "").strip()
        title = (sec.get("title") or "").strip()
        if desc:
            parts.append(desc)
        elif title:
            parts.append(title)
    text = "\n".join(parts).strip()
    if len(text) < 8:
        # Fallback: facility comment snippets / tip titles
        fac = data.get("hotelFacility") or {}
        comments = ((fac.get("comment") or {}).get("commentList") or []) if isinstance(fac, dict) else []
        tips = ((data.get("hotelReservationTips") or {}).get("tipList") or [])
        bits: list[str] = []
        for c in comments[:5]:
            if isinstance(c, str) and c.strip():
                bits.append(c.strip().strip("“”\""))
            elif isinstance(c, dict):
                t = (c.get("content") or c.get("comment") or c.get("title") or "").strip()
                if t:
                    bits.append(t.strip("“”\""))
        for t in tips[:3]:
            if isinstance(t, dict) and t.get("title"):
                bits.append(str(t["title"]))
        # Policy blurbs as last resort
        if not bits:
            policy = data.get("hotelPolicy") or {}
            for item in (policy.get("policyItems") or [])[:3]:
                if not isinstance(item, dict):
                    continue
                title = (item.get("title") or item.get("policyTitle") or "").strip()
                desc = (item.get("desc") or item.get("content") or "").strip()
                if title and desc:
                    bits.append(f"{title}：{desc[:80]}")
                elif desc:
                    bits.append(desc[:120])
                elif title:
                    bits.append(title)
        text = "；".join(bits).strip()
    if len(text) < 8:
        return None
    return text[:800]


def images_from_introduction(additional: dict[str, Any] | None) -> list[str]:
    """Pull pictureList from hotelIntroduction.sectionList (album URLs often stripped)."""
    if not additional:
        return []
    data = additional.get("data") or additional
    intro = data.get("hotelIntroduction") or {}
    if not isinstance(intro, dict):
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for sec in intro.get("sectionList") or []:
        if not isinstance(sec, dict):
            continue
        for u in sec.get("pictureList") or []:
            if not isinstance(u, str) or not u.startswith("http"):
                continue
            if u in seen:
                continue
            seen.add(u)
            urls.append(u)
            if len(urls) >= 24:
                return urls
    return urls


def facilities_from_additional(additional: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten hotelFacility.category into [{name, tag?}]."""
    if not additional:
        return []
    data = additional.get("data") or additional
    fac = data.get("hotelFacility") or {}
    if not isinstance(fac, dict):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cat in fac.get("category") or []:
        if not isinstance(cat, dict):
            continue
        cat_name = cat.get("categoryName") or ""
        for item in cat.get("facilityList") or []:
            if not isinstance(item, dict):
                continue
            name = (
                item.get("facilityName")
                or item.get("name")
                or item.get("showName")
                or item.get("facilityTitle")
                or item.get("facilityShowName")
                or item.get("title")
            )
            # some payloads nest the label
            if not name and isinstance(item.get("facilityInfo"), dict):
                fi = item["facilityInfo"]
                name = fi.get("name") or fi.get("facilityName") or fi.get("title")
            if not name:
                continue
            name = str(name).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            row: dict[str, Any] = {"name": name}
            if cat_name:
                row["tag"] = str(cat_name)
            out.append(row)
            if len(out) >= 60:
                return out
    # fallback: feature highlight names
    if not out:
        intro = data.get("hotelIntroduction") or {}
        for card in (intro.get("highLightCardList") or []) if isinstance(intro, dict) else []:
            if isinstance(card, dict) and card.get("tagName"):
                n = str(card["tagName"]).strip()
                if n and n not in seen:
                    seen.add(n)
                    out.append({"name": n})
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
    if len(images) < 4:
        intro_imgs = images_from_introduction(additional)
        if intro_imgs:
            seen = set(images)
            for u in intro_imgs:
                if u not in seen:
                    seen.add(u)
                    images.append(u)
    api_intro = introduction_from_additional(additional)
    api_fac = facilities_from_additional(additional)
    page_fac = page.get("facilities") or []
    base_fac = base.get("facilities") or []
    features = page.get("features") or base.get("features") or []
    if not features and api_fac:
        features = [{"name": x["name"]} for x in api_fac[:12]]

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
        "features": features,
        "facilities": page_fac or api_fac or base_fac,
        "introduction": page.get("introduction") or api_intro or base.get("introduction"),
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
