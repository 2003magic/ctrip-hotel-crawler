from __future__ import annotations

from typing import Any

from ctrip_hotel.hotel_parse import merge_hotel_full


def normalize_hotels_from_list_api(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") or {}
    hotels = data.get("hotelList") or []
    rows: list[dict[str, Any]] = []
    for item in hotels:
        info = item.get("hotelInfo") or item
        summary = info.get("summary") or {}
        name_info = info.get("nameInfo") or {}
        star = info.get("hotelStar") or {}
        comment = info.get("commentInfo") or {}
        position = info.get("positionInfo") or {}
        hotel_id = summary.get("hotelId") or info.get("hotelId")
        if not hotel_id:
            continue
        rows.append(
            {
                "hotel_id": hotel_id,
                "name": name_info.get("name") or info.get("name"),
                "en_name": name_info.get("enName"),
                "star": star.get("star"),
                "score": comment.get("commentScore"),
                "comment_count": comment.get("commenterNumber")
                or comment.get("commentCount"),
                "address": position.get("address") or position.get("positionDesc"),
                "zone": position.get("zoneNames") or position.get("areaName"),
            }
        )
    return rows


def normalize_hotels_from_dom(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for c in cards:
        hid = c.get("hotel_id")
        if not hid:
            continue
        rows.append(
            {
                "hotel_id": int(hid) if str(hid).isdigit() else hid,
                "name": c.get("name"),
                "en_name": None,
                "star": c.get("star"),
                "score": c.get("score"),
                "comment_count": c.get("comment_count"),
                "address": c.get("address"),
                "zone": c.get("zone"),
            }
        )
        # keep optional list fields for later merge
    return rows


def _img_urls(picture_info: Any) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    if not isinstance(picture_info, list):
        return urls
    for p in picture_info:
        if not isinstance(p, dict):
            continue
        u = p.get("url") or p.get("bigPicUrl") or p.get("smallPicUrl")
        if not u or not isinstance(u, str):
            continue
        # skip non-image / placeholder
        if "viewall" in u.lower() or "查看" in u:
            continue
        if u in seen:
            continue
        seen.add(u)
        urls.append(u)
    return urls


def _facility_item(sub: dict[str, Any]) -> dict[str, Any]:
    additions = sub.get("additionInfo") or []
    notes = []
    free = None
    for a in additions:
        if not isinstance(a, dict):
            continue
        content = a.get("infoContent") or ""
        if "免费" in content:
            free = True
        if content:
            notes.append(content)
    # Ctrip UI often shows green「免费」when freeType == 0
    if free is None and sub.get("freeType") == 0:
        free = True
    # unavailable often still listed; icon slash not in JSON — use explicit flags if any
    available = True
    if sub.get("isNormalShow") == 0:
        available = False
    return {
        "name": sub.get("title"),
        "free": free,
        "note": "；".join(notes) if notes else None,
        "available": available,
    }


def _room_from_physic(proom: dict[str, Any]) -> dict[str, Any]:
    fac = proom.get("faciltityInfo") or {}
    categories = []
    for block in fac.get("list") or []:
        if not isinstance(block, dict):
            continue
        items = [
            _facility_item(s)
            for s in (block.get("subList") or [])
            if isinstance(s, dict) and s.get("title")
        ]
        if items:
            categories.append({"title": block.get("title"), "items": items})

    bed = (proom.get("bedInfo") or {}).get("title")
    window = (proom.get("windowInfo") or {}).get("title")
    smoke = (proom.get("smokeInfo") or {}).get("title")
    area = (proom.get("areaInfo") or {}).get("title")
    floor = (proom.get("floorInfo") or {}).get("title")
    wifi = (proom.get("wifiInfo") or {}).get("title")
    brief = []
    for f in proom.get("physicalFacilityList") or []:
        if isinstance(f, dict) and f.get("title"):
            brief.append({"icon": f.get("icon"), "title": f.get("title")})

    return {
        "room_id": proom.get("id"),
        "room_name": proom.get("name"),
        "images": _img_urls(proom.get("pictureInfo")),
        "bed": bed,
        "window": window,
        "smoke": smoke,
        "area": area,
        "floor": floor,
        "wifi": wifi,
        "extra_bed": None,
        "brief_facilities": brief,
        "detail_categories": categories,
        "offers": [],
    }


def _offer_from_sale(sroom: dict[str, Any]) -> dict[str, Any]:
    booking = sroom.get("bookingStatusInfo") or {}
    meal = sroom.get("mealInfo") or {}
    cancel = sroom.get("cancelInfo") or {}
    confirm = sroom.get("confirmInfo") or {}
    pay = sroom.get("paymentInfo") or {}
    guest = sroom.get("guestCountInfo") or {}
    left = booking.get("remainRoomQuantity")
    if left is not None and int(left) >= 999:
        left = None
    return {
        "offer_id": sroom.get("id"),
        "meal": meal.get("title"),
        "cancel": cancel.get("title"),
        "confirm": confirm.get("title"),
        "pay": pay.get("subTitle") or pay.get("paymentTitleNew"),
        "occupancy": guest.get("guestCount"),
        "left": left,
    }


def build_rooms_from_api(api_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not api_payload:
        return []
    data = api_payload.get("data") or {}
    if data.get("htlSpiderActionErrorCode"):
        return []
    physic = data.get("physicRoomMap") or {}
    sale = data.get("saleRoomMap") or {}
    if not isinstance(physic, dict):
        return []

    rooms_by_id: dict[Any, dict[str, Any]] = {}
    for pid, proom in physic.items():
        if not isinstance(proom, dict):
            continue
        room = _room_from_physic(proom)
        rid = room["room_id"] or pid
        room["room_id"] = rid
        rooms_by_id[str(rid)] = room

    if isinstance(sale, dict):
        for sroom in sale.values():
            if not isinstance(sroom, dict):
                continue
            phys_id = sroom.get("physicalRoomId")
            key = str(phys_id) if phys_id is not None else None
            if not key or key not in rooms_by_id:
                continue
            rooms_by_id[key]["offers"].append(_offer_from_sale(sroom))

    # dedupe offers inside room
    out = []
    for room in rooms_by_id.values():
        seen = set()
        uniq_offers = []
        for o in room["offers"]:
            sig = (o.get("meal"), o.get("cancel"), o.get("confirm"), o.get("pay"), o.get("occupancy"))
            if sig in seen:
                continue
            seen.add(sig)
            uniq_offers.append(o)
        room["offers"] = uniq_offers
        out.append(room)

    out.sort(key=lambda r: str(r.get("room_name") or ""))
    return out


def merge_hotel_info(
    base: dict[str, Any] | None,
    page_hotel: dict[str, Any] | None,
) -> dict[str, Any]:
    base = dict(base or {})
    page = dict(page_hotel or {})
    images = list(page.get("images") or [])
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
        "image_count": page.get("image_count") or len(images),
        "features": page.get("features") or [],
        "facilities": page.get("facilities") or [],
        "introduction": page.get("introduction"),
        "nearby": page.get("nearby")
        or {"metro": [], "airport": [], "train": []},
    }
    return hotel


def fill_hotel_images_from_rooms(doc: dict[str, Any]) -> dict[str, Any]:
    """If hotel gallery empty, assemble covers from room photos (deduped)."""
    hotel = doc.get("hotel") or {}
    if hotel.get("images"):
        return doc
    seen: set[str] = set()
    imgs: list[str] = []
    for room in doc.get("rooms") or []:
        for u in room.get("images") or []:
            if u in seen:
                continue
            seen.add(u)
            imgs.append(u)
            if len(imgs) >= 12:
                break
        if len(imgs) >= 12:
            break
    hotel["images"] = imgs
    hotel["image_count"] = hotel.get("image_count") or len(imgs)
    doc["hotel"] = hotel
    return doc


def build_hotel_document(
    *,
    hotel_meta: dict[str, Any],
    page_hotel: dict[str, Any] | None,
    fetch_result: dict[str, Any],
    check_in: str,
    check_out: str,
) -> dict[str, Any]:
    api = fetch_result.get("api") if isinstance(fetch_result, dict) else None
    rooms = build_rooms_from_api(api)
    # fallback: thin DOM rooms if API empty
    if not rooms:
        for dr in fetch_result.get("dom_rooms") or []:
            rooms.append(
                {
                    "room_id": None,
                    "room_name": dr.get("room_name"),
                    "images": dr.get("images") or [],
                    "bed": dr.get("bed"),
                    "window": dr.get("window"),
                    "smoke": dr.get("smoke"),
                    "area": dr.get("area"),
                    "floor": dr.get("floor"),
                    "wifi": dr.get("wifi"),
                    "extra_bed": None,
                    "brief_facilities": [],
                    "detail_categories": [],
                    "offers": dr.get("sales")
                    or [
                        {
                            "meal": None,
                            "cancel": None,
                            "confirm": None,
                            "pay": None,
                            "occupancy": None,
                            "left": None,
                        }
                    ],
                }
            )

    hotel = merge_hotel_full(
        base=hotel_meta,
        page_hotel=page_hotel,
        album=fetch_result.get("album") if isinstance(fetch_result, dict) else None,
        additional=fetch_result.get("additional")
        if isinstance(fetch_result, dict)
        else None,
    )
    doc = {
        "hotel_id": hotel.get("hotel_id") or hotel_meta.get("hotel_id"),
        "check_in": check_in,
        "check_out": check_out,
        "source": fetch_result.get("source"),
        "hotel": hotel,
        "rooms": rooms,
    }
    return fill_hotel_images_from_rooms(doc)


# Backward-compatible helpers used by diagnose
def normalize_rooms(
    hotel_id: int | str,
    result: dict[str, Any],
    *,
    check_in: str,
    check_out: str,
) -> list[dict[str, Any]]:
    doc = build_hotel_document(
        hotel_meta={"hotel_id": hotel_id},
        page_hotel=result.get("page_hotel"),
        fetch_result=result,
        check_in=check_in,
        check_out=check_out,
    )
    flat = []
    for room in doc["rooms"]:
        for offer in room.get("offers") or [None]:
            flat.append(
                {
                    "hotel_id": hotel_id,
                    "room_id": room.get("room_id"),
                    "room_name": room.get("room_name"),
                    "bed": room.get("bed"),
                    "window": room.get("window"),
                    "images": len(room.get("images") or []),
                    "meal": (offer or {}).get("meal"),
                    "cancel": (offer or {}).get("cancel"),
                    "occupancy": (offer or {}).get("occupancy"),
                    "error": None if room.get("room_name") else "no_room_data",
                }
            )
    return flat or [
        {
            "hotel_id": hotel_id,
            "check_in": check_in,
            "check_out": check_out,
            "error": "no_room_data",
            "room_name": None,
        }
    ]
