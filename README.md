# ctrip-hotel-crawler

抓取携程**酒店信息 + 房态/房价**。只有一套程序、一个 `data/` 输出目录。

支持两种抓取模式（`config.yaml` 里的 `mode`）：
- **`api`（推荐）**：列表走纯 HTTP（curl_cffi 模拟浏览器 TLS 指纹），房态走「单个无头浏览器页面 + 页面内 API 请求」。资源占用极低，实测约 **0.8 秒/家**。
- **`browser`（原方案）**：多浏览器 worker 逐个导航酒店详情页解析 DOM。

## 背景 / 逆向结论

走 `m.ctrip.com/restapi/soa2/...` 手机端/H5 JSON，而不是解析 PC HTML。本机实测（2026-08）：

| 接口 | 纯 HTTP | 说明 |
|------|---------|------|
| `soa2/31454/.../fetchHotelList` | ✅ 可通 | 列表；参数 `destination:{type:2,geo:{cityId}}` |
| `soa2/33278/.../getHotelRoomListInland` | ❌ 203/4030 | 房态；被 `phantom-token` 签名保护 |
| 图片 `soa2/12465/h5-json/getHotelAlbumPicture` | ❌ | 同样受签名保护 |
| 周边 `soa2/33278/.../getDetailAdditionalInfo` | ❌ | 同样受签名保护 |

**关键机制**：房态接口依赖请求头 `phantom-token`（页面 JS `window.signature()` 生成，格式 `1004-h5common-...`）。它由浏览器 JS 动态生成、绑定首次请求的酒店，纯 `requests`/`curl_cffi` 无法跨酒店复用。

**可行方案**：保持 **1 个无头浏览器页面**预热，捕获页面自动发出的完整请求模板（URL + POST body），然后**在页面 JS 上下文内**用 `fetch` 重放，只改 `hotelId`。浏览器 JS 会为每个请求重新生成有效签名，实测串行 100% 成功。

> ⚠️ 并发踩坑：在页面内用 `Promise.all` 并发请求会触发风控返回空数据。**必须串行**。要提速就多开几个浏览器页面（`workers`），每个页面内部串行。

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
- `workers`：并行浏览器页面数（API 模式）
- `delay_ms`：酒店间隔（API 模式建议 600–1000ms，降低风控）
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
