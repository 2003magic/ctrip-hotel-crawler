from __future__ import annotations

from typing import Any


REQUIRED_HOTEL = (
    "name",
    "address",
    "images",
    "score",
    "introduction",
    "facilities",
    "nearby",
)


def hotel_gaps(hotel: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    if not hotel.get("name"):
        gaps.append("name")
    if not hotel.get("address"):
        gaps.append("address")
    if not hotel.get("images"):
        gaps.append("images")
    if hotel.get("score") is None and hotel.get("review_count") is None:
        gaps.append("score/review_count")
    if not hotel.get("introduction"):
        gaps.append("introduction")
    fac = hotel.get("facilities") or []
    feat = hotel.get("features") or []
    if not fac and not feat:
        gaps.append("facilities/features")
    nearby = hotel.get("nearby") or {}
    if not any(nearby.get(k) for k in ("metro", "airport", "train", "other")):
        gaps.append("nearby")
    return gaps


def room_gaps(rooms: list[dict[str, Any]]) -> list[str]:
    if not rooms:
        return ["rooms"]
    gaps = []
    no_img = sum(1 for r in rooms if not r.get("images"))
    no_cat = sum(1 for r in rooms if not r.get("detail_categories"))
    if no_img:
        gaps.append(f"rooms_without_images:{no_img}")
    if no_cat:
        gaps.append(f"rooms_without_detail:{no_cat}")
    return gaps


def document_gaps(doc: dict[str, Any]) -> list[str]:
    return hotel_gaps(doc.get("hotel") or {}) + room_gaps(doc.get("rooms") or [])


def is_complete(doc: dict[str, Any]) -> bool:
    # allow soft gaps on introduction/features if everything else ok? User said 补满 — require all.
    return not document_gaps(doc)
