"""Canonical document shape for restore/preview.

{
  hotel_id, check_in, check_out,
  hotel: {
    hotel_id, name, star, address,
    score, score_label, review_count, review_snippet,
    images: [url...],          # real photos only
    image_count,
    features: [{name}],
    facilities: [{name, tag}],
    introduction,
    nearby: {metro:[], airport:[], train:[]}
  },
  rooms: [{
    room_id, room_name,
    images: [url...],          # no "+N / 查看全部" tile
    bed, window, smoke, area, floor, wifi,
    extra_bed,
    brief_facilities: [{icon, title}],
    detail_categories: [{
      title,
      items: [{name, free, note, available}]
    }],
    offers: [{
      meal, cancel, confirm, pay, occupancy, left
    }]
  }]
}
"""
