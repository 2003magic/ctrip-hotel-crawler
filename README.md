# ctrip-hotel-crawler (minimal)

Single-file API crawler: hotel list + rooms + enrich + intl HKD prices.

## Setup

`ash
pip install -r requirements.txt
playwright install chromium
copy config.example.yaml config.yaml
`

Edit config.yaml (set intl_proxy if you need overseas prices).

## Run

`ash
python crawl.py
python crawl.py --max-hotels 5 --city-id 1
python crawl.py --no-intl-price
python crawl.py --no-skip-done
`

Output: data/<timestamp>/hotels/*.json
