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
    nearby: {metro:[], airport:[], train:[]},
    # --- international-site price info (intl_price: true) ---
    min_price_hkd,            # 港币最低价（酒店级）
    min_price_cny,            # 按实时汇率折算人民币
    price_info: {
      currency: "HKD",
      exchange_rate,          # HKD->CNY
      exchange_currency: "CNY",
      rooms: [{               # 每个总房型（物理房型）
        physical_room_id, room_name,
        start_price_hkd, start_price_cny,
        plans: [{             # 每个子房型（销售方案）
          plan_id, room_name,
          price_hkd, price_cny,
          summary,            # 房型摘要（套餐组合差异）
          meal, cancel, confirm, occupancy, left,
          folded              # 是否默认折叠
        }]
      }]
    }
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
    }],
    prices: [{                 # 国际版该总房型下的方案价格（折叠信息也在）
      plan_id, room_name, price_hkd, price_cny,
      summary, meal, cancel, confirm, occupancy, left, folded
    }]
  }]
}
"""
