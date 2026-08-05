# 携程酒店价格抓取可行性验证（2026-08-05 实测）

> 目标：**不登录**的前提下，能否从 `ctrip-hotel-crawler` 现有的 SOA2 接口拿到酒店价格？

## 结论

**部分能拿，逐房型价格拿不到。**

| 价格类型 | 是否可拿 | 来源接口 | 说明 |
|---------|---------|---------|------|
| 酒店底价（"¥X 起"） | ✅ **可拿** | `getHotelRoomListInland` → `data.hotelDetailBarInfo.price` | 未登录直接返回，实测 ¥319 |
| 预售套餐价 | ✅ **可拿** | `getDetailAdditionalInfo` → `data.inStoreProduct.productList[].price` | 未登录直接返回，实测 ¥578 / ¥718（"双晚可拆分"套餐） |
| **逐房型实时价** | ❌ **不可拿** | `getHotelRoomListInland` → `saleRoomMap[].priceStr` | 未登录固定返回 `'?'`，被遮蔽 |
| 会员/折扣价 | ❌ **不可拿** | 同上 | 卡片显示"登录看低价" |

## 实测过程

### 1. 列表接口（fetchHotelList）
纯 HTTP（curl_cffi impersonate=chrome）可直接调通，返回 25 家。但 `roomInfo` **不含任何价格字段**（只有房型名、床型、payType）。列表只提供元信息。

### 2. 房态接口（getHotelRoomListInland）
需要 `phantom-token`（现有爬虫已解决：预热浏览器捕获模板 + 纯 HTTP 复用）。
未登录响应中：
- `saleRoomMap.*.priceStr = '?'` —— 逐房型价格被遮蔽
- `saleRoomMap.*.bookBtn.text = '解锁'`
- `data.hotelDetailBarInfo = { price: "319", currency: "¥", ... }` —— **酒店底价真实返回**
- 请求 body 的 `functionOptions` 含 `HidePromotionForUnloginUser`（对未登录用户隐藏促销）、`HideCUGRoomForUnloginUser`

### 3. 点击"解锁" / "登录看低价"
浏览器页面中房型卡片显示"登录看低价"（不是可点的解锁按钮）。点击后**直接跳转登录页** `accounts.ctrip.com/H5Login/Index`，并弹出"阅读并同意携程的《服务协议》《个人信息保护政策》，未注册手机号将自动注册"。

→ 逐房型实时价被登录墙保护，**不登录无法绕过**。

### 4. 页面显示 ¥578 / ¥718 的真相
不是逐房型价，而是 `getDetailAdditionalInfo` → `inStoreProduct` 里的**预售套餐**（"如家惬意一夏全国通兑双晚578可拆分"）。这个接口同样**不需要 phantom-token**，纯 HTTP 即可拿。

## 对开发的启示

**现状**：`normalize.py` 的 `_offer_from_sale()` 已读取 `sroom.get("priceStr")`，但因为值是 `'?'`，产出的是占位符。`client.py` 的 `_click_unlock_offers()` 点的"解锁优惠"在当前页面已不存在（已改为"登录看低价"）。

**可选增强方向**：
1. **拿酒店底价**：在 `build_rooms_from_api` / `build_fetch_result` 中解析 `hotelDetailBarInfo.price`，写入 `hotel.min_price`。改动小、立即可用。
2. **拿预售套餐价**：解析 `getDetailAdditionalInfo` 的 `inStoreProduct.productList`，作为 `hotel.packages` 附加信息。纯 HTTP、无需 token。
3. **逐房型实时价**：只能登录后拿。若必须，需要处理携程登录态（cookie）复用，涉及账号与合规风险，超出"不登录"前提。

## 合规提醒
仅供个人学习/研究，请遵守携程服务条款与当地法律，控制抓取频率。
