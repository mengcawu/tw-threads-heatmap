#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回補：三大法人買賣金額統計（全市場總額，非個股別），對齊 data/stock_day_common.csv
的交易日範圍。

資料來源（TWSE 官方公開資料，逐日回補）：
  https://www.twse.com.tw/rwd/zh/fund/BFI82U?dayDate=YYYYMMDD&type=day&response=json
      三大法人買賣金額統計表，全市場（不分個股）買進金額／賣出金額／買賣差額，
      單位為新台幣元。回傳6列：自營商(自行買賣)、自營商(避險)、投信、
      外資及陸資(不含外資自營商)、外資自營商、合計。

彙整規則：自營商(自行買賣) + 自營商(避險) 合併為「自營商」；
          外資及陸資(不含外資自營商) + 外資自營商 合併為「外資」。
          （分類方式跟 backfill_institutional.py 的 DealerNet／ForeignNet
          定義一致，方便日後串接比對；本檔是全市場總額金額，
          institutional.csv 是個股買賣超股數，兩者顆粒度不同。）

對齊策略：跟 backfill_institutional.py／backfill_margin.py 相同，不獨立往回
試探交易日，而是直接沿用 data/stock_day_common.csv 目前已回補好的交易日
清單逐一查詢。若某個已知交易日查不到資料（stat!=OK），如實記錄為缺漏，
不補假資料。

存儲：data/institutional_flow.csv，欄位
  Date,ForeignBuy,ForeignSell,ForeignNet,TrustBuy,TrustSell,TrustNet,
  DealerBuy,DealerSell,DealerNet,TotalBuy,TotalSell,TotalNet
單位皆為新台幣元（原始金額，未換算億元；顯示時再換算，見 generate_caption.py）。
每次執行只補目前缺少的交易日，已有的日期不重查（BFI82U 是歷史金額，不會
事後變動，不需要每天重抓全部30天）。
"""

import csv
import os
import sys
import time

import requests

import ingest_stock_day as base

WWW_BASE = "https://www.twse.com.tw"
TIMEOUT = 20
RETRIES = 3
REQUEST_DELAY_SEC = 1.5

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(REPO_ROOT, "data")
STORE_PATH = os.path.join(DATA_DIR, "institutional_flow.csv")
PRICE_PATH = os.path.join(DATA_DIR, "stock_day_common.csv")

FIELDNAMES = [
    "Date",
    "ForeignBuy", "ForeignSell", "ForeignNet",
    "TrustBuy", "TrustSell", "TrustNet",
    "DealerBuy", "DealerSell", "DealerNet",
    "TotalBuy", "TotalSell", "TotalNet",
]


def fetch_bfi82u_day(date_str: str):
    url = f"{WWW_BASE}/rwd/zh/fund/BFI82U"
    params = {"dayDate": date_str, "type": "day", "response": "json"}
    last_err = None
    for _ in range(RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            last_err = e
            time.sleep(2)
    host = base._host(url)
    print(f"[錯誤] 無法連線至 {host}（{url}，date={date_str}）：{last_err}", file=sys.stderr)
    base.blocked_hosts.append(host)
    return None


def to_int(s):
    if s in (None, "", "--"):
        return None
    try:
        return int(str(s).replace(",", ""))
    except ValueError:
        return None


def parse_bfi82u(payload):
    """回傳 dict（欄位同 FIELDNAMES，不含 Date）或 None（查無資料）。"""
    if not payload or payload.get("stat") != "OK":
        return None

    rows = {r[0]: r for r in payload.get("data", [])}

    def get(label):
        r = rows.get(label)
        if not r:
            return None, None
        return to_int(r[1]), to_int(r[2])  # 買進金額, 賣出金額

    def add(*vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None

    def net(buy, sell):
        return (buy - sell) if buy is not None and sell is not None else None

    dealer_self_buy, dealer_self_sell = get("自營商(自行買賣)")
    dealer_hedge_buy, dealer_hedge_sell = get("自營商(避險)")
    trust_buy, trust_sell = get("投信")
    foreign_buy, foreign_sell = get("外資及陸資(不含外資自營商)")
    foreign_dealer_buy, foreign_dealer_sell = get("外資自營商")
    total_buy, total_sell = get("合計")

    dealer_buy = add(dealer_self_buy, dealer_hedge_buy)
    dealer_sell = add(dealer_self_sell, dealer_hedge_sell)
    foreign_all_buy = add(foreign_buy, foreign_dealer_buy)
    foreign_all_sell = add(foreign_sell, foreign_dealer_sell)

    if total_buy is None or total_sell is None:
        return None

    return {
        "ForeignBuy": foreign_all_buy, "ForeignSell": foreign_all_sell,
        "ForeignNet": net(foreign_all_buy, foreign_all_sell),
        "TrustBuy": trust_buy, "TrustSell": trust_sell,
        "TrustNet": net(trust_buy, trust_sell),
        "DealerBuy": dealer_buy, "DealerSell": dealer_sell,
        "DealerNet": net(dealer_buy, dealer_sell),
        "TotalBuy": total_buy, "TotalSell": total_sell,
        "TotalNet": net(total_buy, total_sell),
    }


def load_target_dates():
    with open(PRICE_PATH, newline="", encoding="utf-8") as f:
        return sorted({row["Date"] for row in csv.DictReader(f)})


def load_existing():
    if not os.path.exists(STORE_PATH):
        return []
    with open(STORE_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_store(rows: list):
    os.makedirs(DATA_DIR, exist_ok=True)
    rows_sorted = sorted(rows, key=lambda r: r["Date"])
    with open(STORE_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows_sorted)


def main():
    target_dates = load_target_dates()
    existing_rows = load_existing()
    existing_by_date = {r["Date"]: r for r in existing_rows}

    missing_dates = [d for d in target_dates if d not in existing_by_date]
    fetched = {}
    skipped = []
    for i, iso_date in enumerate(missing_dates):
        date_str = iso_date.replace("-", "")
        payload = fetch_bfi82u_day(date_str)
        parsed = parse_bfi82u(payload)
        if parsed is None:
            skipped.append(iso_date)
        else:
            row = {"Date": iso_date, **parsed}
            fetched[iso_date] = {k: ("" if v is None else v) for k, v in row.items()}
        if i < len(missing_dates) - 1:
            time.sleep(REQUEST_DELAY_SEC)

    merged = {r["Date"]: r for r in existing_rows}
    merged.update(fetched)
    kept = [merged[d] for d in target_dates if d in merged]
    write_store(kept)

    print(f"目標交易日數：{len(target_dates)}")
    print(f"新抓取：{len(fetched)} 天；缺漏（查無資料）：{len(skipped)} 天 {skipped}")
    print(f"存儲檔案路徑：{STORE_PATH}")
    print(f"存儲目前總列數：{len(kept)}")

    if base.blocked_hosts:
        print(f"[警告] 以下網域連線失敗：{sorted(set(base.blocked_hosts))}", file=sys.stderr)


if __name__ == "__main__":
    main()
