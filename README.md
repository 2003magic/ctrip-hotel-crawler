# ctrip-hotel-crawler

抓取携程**酒店信息 + 房态/房价**。只有一套程序、一个 `data/` 输出目录。

支持两种抓取模式（`config.yaml` 里的 `mode`）：
- **`api`（推荐）**：列表 + 房态全部走**纯 HTTP**（curl_cffi 模拟 Chrome TLS 指纹）。仅启动时用一次无头浏览器获取 `phantom-token`，之后**多线程并发抓取**，实测 **0.15 秒/家**。
- **`browser`（原方案）**：多浏览器 worker 逐个导航酒店详情页解析 DOM。

## 背景 / 逆向结论

走 `m.ctrip.com/restapi/soa2/...` 手机端/H5 JSON，而不是解析 PC HTML。本机实测（2026-08）：

| 接口 | 纯 HTTP | 说明 |
|------|---------|------|
| `soa2/31454/.../fetchHotelList` | ✅ 可通 | 列表；参数 `destination:{type:2,geo:{cityId}}` |
| `soa2/33278/.../getHotelRoomListInland` | ✅ 可通 | 房态；需带有效的 `phantom-token` |
| 图片 `soa2/12465/h5-json/getHotelAlbumPicture` | ⚠️ | 需带 `phantom-token`（列表自带封面图可用） |
| 周边 `soa2/33278/.../getDetailAdditionalInfo` | ⚠️ | 需带 `phantom-token` |

**关键机制**：房态接口依赖请求头 `phantom-token`（页面 JS `window.signature()` 生成，格式 `1004-h5common-<base64>`）。它是**短期有效**（约 20–60 秒）的签名 token，但**不绑定酒店**——同一个 token 可以抓任意多家酒店。

**方案**：启动时用**一个无头浏览器页面**预热，捕获页面自动发出的房态请求模板（URL + POST body + headers 里的 `phantom-token` + cookie），之后**完全退出浏览器**，用 curl_cffi 纯 HTTP 多线程复用该 token 批量抓房态。token 过期时自动重新预热。

实测性能（沙箱数据中心 IP）：**并发 10 家 1.4 秒，10/10 成功**；串行 15 家 11.5 秒。家宽 IP 下更快更稳。

**不需要登录携程账号。** 默认不采集价格，只抓房型/床型/窗户/面积/早餐/取消/入住人数等。

## 快速开始

```powershell
cd $env:USERPROFILE\Desktop\ctrip-hotel-crawler
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
python -m ctrip_hotel init
python -m ctrip_hotel crawl --mode api
```

首次请保持 `headed: true`。若弹出**人机验证/滑块**（仍不必登录），点一下即可；默认最多等 `verify_wait_sec: 180` 秒。通过后可将 `headed` 改为 `false` 无头运行。

## 配置

复制自 `config.example.yaml` → `config.yaml`：

- `mode`：`api` 或 `browser`
- `city_id`：携程城市 ID（1 北京，2 上海…）
- `check_in` / `check_out`：入住日期
- `max_hotels`：本次抓房态的酒店数
- `workers`：并行 worker 数（API 模式每个 worker 一个 token）
- `api_workers`：每个 worker 内并发线程数（API 模式建议 8–16）
- `delay_ms`：每批间隔（API 模式建议 0–200ms）
- `seed_hotel_id`：API 预热用的任意酒店 ID
- `output_dir`：固定为 `data`

## 输出（每次一个 run 子目录）

```
data/
  20260805_120000/
    catalog.json           # 预览列表
    hotels/
      115543863.json       # 单酒店完整结构（酒店信息+房型+图片URL+设施分类+售卖政策）
    workers/*/raw/         # 原始抓取备份
    config.used.json
  latest.json
```

还原预览网站：

```powershell
python -m ctrip_hotel preview
# 打开 http://127.0.0.1:8765/
```

## 价格（国际版 hk.trip.com）

国内版 `getHotelRoomListInland` 未登录时逐房型价格是 `'?'`（登录墙）。
国际版 `getHotelRoomListOversea` **可以**返回逐房型港币价，但 WhaleGuard 要求：

1. `phantom-token = window.signature(<精确 POST body>)`（**一次性 + 绑定 hotelId**）
2. 浏览器预热时 **abort** 首个房态请求（避免 token 被页面消耗）
3. **小批签发 → 立刻 HTTP**（默认批大小=`intl_workers`），避免 token 等太久过期
4. **4030 自动重签重试**；**430 / 签名失效自动 rewarm** 后再试（`intl_http_retries` / `intl_rewarm_after`）
5. HTTP 必须走与浏览器相同的**境外代理**；签名浏览器建议**有头**（无头易 HTTP 430）

旧误区：捕获页面已发出的 token 再重放 → 必 `4030`（token 已用过）。

```powershell
python intl_verify.py --proxy http://127.0.0.1:7897
python intl_verify.py --proxy http://127.0.0.1:7897 --max-hotels 3
python -m ctrip_hotel crawl --mode api --intl-price --proxy http://127.0.0.1:7897
```

输出 `hotel.price_info`：`总房型 → 子房型(方案)`，含 `price_hkd` / `price_cny` / `display_price` 等。
港币按实时汇率折算人民币（失败回退 0.9）。

**备用价格源**（不依赖国际版房态）：
- 国内版底价：`hotelDetailBarInfo.price`
- 国内版预售套餐：`getDetailAdditionalInfo` → `inStoreProduct.productList`

## 多开 / 分组 / 防重复

- `workers: 2`：API 模式 = 2 个并行无头页面；browser 模式 = 2 个浏览器
- 酒店按 **round-robin** 分给各组，互不重叠
- `skip_done: true`：成功写过的酒店记入 `data/state/done.jsonl`，下次自动跳过  
  键：`城市:酒店ID:入住:离店`

```powershell
python -m ctrip_hotel crawl --mode api --workers 2 --max-hotels 6
python -m ctrip_hotel status
python -m ctrip_hotel crawl --no-skip-done   # 强制重抓
```

## 命令

```powershell
python -m ctrip_hotel init
python -m ctrip_hotel diagnose
python -m ctrip_hotel crawl --mode api --max-hotels 5
python -m ctrip_hotel crawl --mode api --city-id 2 --workers 2
```

## 说明

- 仅供个人学习/研究；请遵守携程服务条款与当地法律，控制频率。
- 不接入多套 GitHub 老爬虫，避免接口/数据各写一套、互相打架。
