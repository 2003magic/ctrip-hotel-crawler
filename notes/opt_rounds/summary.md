# Optimization rounds (continued R6-R8)

Prior best (R5b): 16 hotels wall 21.0s / crawl 13.2s

| Round | N | Wall(s) | Crawl(s) | ok/err | intl | Notes |
|-------|---|---------|----------|--------|------|-------|
| R6 | 16 | 21.0 | 11.6 | 16/0 | 16/16 | intl asset-block warm 2.3s; FX overlap; intl_workers=10 |
| R7 | 16 | 13.8 | 11.6 | 16/0 | 16/16 | list early-stop; API asset-block warm 6.0s; Chromium vs Edge split |
| R8 | 48 | 30.1 | 24.2 | 48/0 | 48/48 | scale OK; ~0.63s/hotel wall; priced 48/48 |

From original R1 baseline 16h/46.3s -> R7 13.8s (~3.4x wall).
48 hotels @ 30s with full intl prices: stable.

Remaining headroom (diminishing):
- intl batch still gates large N (~0.4s/hotel HTTP after warm)
- API warm fixed ~6s (hard floor while needing phantom-token page)
- list paging ~1.5s/page (already early-stops at max_hotels)
