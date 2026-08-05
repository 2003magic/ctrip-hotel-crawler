# ctrip-hotel-crawler

抓取携程**酒店信息 + 房态/房价**。只有一套程序、一个 `data/` 输出目录。

## 为什么不用“多套爬虫”

景区项目那套思路是对的：走 `m.ctrip.com/restapi/soa2/...` 手机端/H5 JSON，而不是解析 PC HTML。

本机实测（2026-08）：

| 接口 | 状态 |
|------|------|
| `soa2/31454/.../fetchHotelList` | 仍是列表接口（同族 `getHotelCommonFilter` 可通） |
| `soa2/33278/.../getHotelRoomListInland` | 仍是房态接口 |
| 裸 `requests` | 列表 `ResultId=201`，房态 `htlSpiderActionErrorCode=4030` |

**不需要登录携程账号。** 你在电脑上未登录也能看酒店详情 /「房间详情」；程序之前提示“登录”，其实是自动化被风控页拦截，不是账号门槛。

做法：用浏览器按真人路径打开列表 → 酒店详情 → 解析页面上的房型（并可点「房间详情」「解锁优惠」）。若页面顺便打出 SOA2 JSON 也会收下，但**以页面可见信息为准**。

## 快速开始

```powershell
cd $env:USERPROFILE\Desktop\ctrip-hotel-crawler
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
python -m ctrip_hotel init
python -m ctrip_hotel diagnose
python -m ctrip_hotel crawl
```

首次请保持 `headed: true`。若弹出**人机验证/滑块**（仍不必登录），点一下即可；默认最多等 `verify_wait_sec: 180` 秒。会话在 `.browser-profile/`。

默认**不点击「解锁优惠」**，也不采集价格；只抓房型/床型/窗户/面积/早餐/取消等。

## 配置

复制自 `config.example.yaml` → `config.yaml`：

- `city_id`：携程城市 ID（1 北京，2 上海…）
- `check_in` / `check_out`：入住日期
- `max_hotels`：本次抓房态的酒店数
- `output_dir`：固定为 `data`（不要另开第二套目录）

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

- `workers: 2`：同时开多个浏览器（各自 `.browser-profile/w0`、`w1`…）
- 酒店按 **round-robin** 分给各组，互不重叠
- `skip_done: true`：成功写过的酒店记入 `data/state/done.jsonl`，下次自动跳过  
  键：`城市:酒店ID:入住:离店`

```powershell
python -m ctrip_hotel crawl --workers 2 --max-hotels 6
python -m ctrip_hotel status
python -m ctrip_hotel crawl --no-skip-done   # 强制重抓
```

## 命令

```powershell
python -m ctrip_hotel init
python -m ctrip_hotel diagnose
python -m ctrip_hotel crawl --max-hotels 5
python -m ctrip_hotel crawl --city-id 2 --workers 2
```

## 说明

- 仅供个人学习/研究；请遵守携程服务条款与当地法律，控制频率。
- 不接入多套 GitHub 老爬虫，避免接口/数据各写一套、互相打架。
