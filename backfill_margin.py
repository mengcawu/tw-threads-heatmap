#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回補：融資融券餘額，對齊 data/stock_day_common.csv 的交易日範圍。

資料來源（TWSE 官方公開資料，逐日回補）：
  https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date=YYYYMMDD&selectType=ALL&response=json
      信用交易統計，個股別，接受 date 參數（已驗證可取歷史，非僅當日快照）。
      回傳兩個表格，個股資料在標題含「融資融券彙總」的表格，16 欄位、
      「買進/賣出/前日餘額/今日餘額/次一營業日限額」等欄名各出現兩次
      （前 8 欄為融資、接續 6 欄為融券、最後為資券互抵/註記），
      無法用 fields.index() 唯一定位，改用已核對過的固定欄位順序：
        0 代號  1 名稱
        2 買進(融資) 3 賣出(融資) 4 現金償還 5 前日餘額(融資) 6 今日餘額(融資)=融資餘額 7 次一營業日限額
        8 買進(融券) 9 賣出(融券) 10 現券償還 11 前日餘額(融券) 12 今日餘額(融券)=融券餘額 13 次一營業日限額
        14 資券互抵 15 註記
      融資增減 = 今日餘額(融資) - 前日餘額(融資)（帳務定義，逐筆核對）。

清洗規則：與 ingest_stock_day.py 完全相同，重用其 classify()，只保留「普通股」。

對齊策略：沿用 data/stock_day_common.csv 目前已回補好的交易日清單逐一查詢，
確保 Code+Date 能與量價資料對上。若某個已知交易日在 MI_MARGN 查不到資料
（stat!=OK），如實記錄為缺漏，不補假資料。

存儲：data/margin.csv，欄位
  Date,Code,Name,MarginBalance,MarginChange,ShortBalance
每次執行會先移除同一 Date 的舊資料再寫入該日新資料（避免重跑造成重複列）。
"""

import csv
import os
import random
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
STORE_PATH = os.path.join(DATA_DIR, "margin.csv")
FIELDNAMES = ["Date", "Code", "Name", "MarginBalance", "MarginChange", "ShortBalance"]

EXPECTED_FIELDS = [
    "代號", "名稱",
    "買進", "賣出", "現金償還", "前日餘額", "今日餘額", "次一營業日限額",
    "買進", "賣出", "現券償還", "前日餘額", "今日餘額", "次一營業日限額",
    "資券互抵", "註記",
]


def fetch_margn_day(date_str: str):
    url = f"{WWW_BASE}/rwd/zh/marginTrading/MI_MARGN"
    params = {"date": date_str, "selectType": "ALL", "response": "json"}
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


def extract_margn_rows(data: dict, iso_date: str):
    table = None
    for t in data.get("tables", []):
        if "融資融券彙總" in (t.get("title") or ""):
            table = t
            break
    if table is None:
        return None

    fields = table.get("fields") or []
    if fields != EXPECTED_FIELDS:
        print(
            f"[警告] {iso_date} MI_MARGN 個股表格欄位與預期不符，略過該日以避免誤讀："
            f"{fields}",
            file=sys.stderr,
        )
        return None

    def num(s):
        s = s.replace(",", "").strip()
        return int(s) if s not in ("", "--") else 0

    rows = []
    for row in table.get("data") or []:
        margin_today = num(row[6])
        margin_prev = num(row[5])
        rows.append(
            {
                "Code": row[0],
                "Name": row[1].strip(),
                "MarginBalance": margin_today,
                "MarginChange": margin_today - margin_prev,
                "ShortBalance": num(row[12]),
            }
        )
    return rows


def load_existing():
    if not os.path.exists(STORE_PATH):
        return []
    with open(STORE_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_store(rows: list):
    os.makedirs(DATA_DIR, exist_ok=True)
    rows_sorted = sorted(rows, key=lambda r: (r["Date"], r["Code"]))
    with open(STORE_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows_sorted)


def merge_for_date(existing_rows: list, new_rows: list, iso_date: str):
    kept = [r for r in existing_rows if r["Date"] != iso_date]
    kept.extend({k: str(v) for k, v in r.items()} for r in new_rows)
    return kept


def main():
    price_days = sorted({r["Date"] for r in base.load_existing()})
    if not price_days:
        print("[失敗] data/stock_day_common.csv 沒有資料，無法決定回補的交易日範圍。", file=sys.stderr)
        sys.exit(1)

    print(f"目標交易日範圍（來自 stock_day_common.csv）：{price_days[0]} ~ {price_days[-1]}，共 {len(price_days)} 天")

    existing_rows = load_existing()
    existing_days = sorted({r["Date"] for r in existing_rows})
    print(f"回補前 margin.csv 已有交易日數：{len(existing_days)}")

    all_rows = list(existing_rows)
    fetched_days = []
    missing_days = []
    excluded_totals = {}
    blocked = False

    for iso in price_days:
        if iso in existing_days:
            fetched_days.append(iso)
            continue

        date_str = iso.replace("-", "")
        data = fetch_margn_day(date_str)
        time.sleep(REQUEST_DELAY_SEC + random.uniform(0, 0.5))

        if data is None:
            blocked = True
            break

        if data.get("stat") != "OK":
            print(f"[缺漏] {iso}：MI_MARGN 回傳 stat={data.get('stat')!r}，該日無融資融券資料。", file=sys.stderr)
            missing_days.append(iso)
            continue

        raw_rows = extract_margn_rows(data, iso)
        if not raw_rows:
            missing_days.append(iso)
            continue

        common_rows, excluded = base.clean(raw_rows)
        for cat, rows in excluded.items():
            excluded_totals[cat] = excluded_totals.get(cat, 0) + len(rows)
        if "無法分類" in excluded:
            for r in excluded["無法分類"][:3]:
                print(f"[警告] {iso} 無法分類代號範例：{r['Code']} {r['Name']}", file=sys.stderr)

        new_rows = [
            {
                "Date": iso,
                "Code": r["Code"],
                "Name": r["Name"],
                "MarginBalance": r["MarginBalance"],
                "MarginChange": r["MarginChange"],
                "ShortBalance": r["ShortBalance"],
            }
            for r in common_rows
        ]

        all_rows = merge_for_date(all_rows, new_rows, iso)
        fetched_days.append(iso)
        print(f"[完成] {iso}：{len(new_rows)} 檔普通股（{len(fetched_days)}/{len(price_days)}）")

    write_store(all_rows)

    stored_days = sorted({r["Date"] for r in all_rows})
    print()
    print("========== margin.csv 回補結果 ==========")
    if blocked:
        print(f"[中止] 連線被擋或重試後仍失敗，回補中途停止：{sorted(set(base.blocked_hosts))}", file=sys.stderr)
    if excluded_totals:
        print("回補期間被排除類別加總：")
        for cat, n in sorted(excluded_totals.items(), key=lambda kv: -kv[1]):
            print(f"  {cat}: {n} 檔次")
    print(f"涵蓋交易日數：{len(stored_days)} / 目標 {len(price_days)}")
    if stored_days:
        print(f"日期範圍：{stored_days[0]} ~ {stored_days[-1]}")
    print(f"總列數：{len(all_rows)}")
    if missing_days:
        print(f"[缺漏] 以下 {len(missing_days)} 個交易日 MI_MARGN 無資料，未補假資料：{missing_days}")
    print(f"存儲檔案路徑：{STORE_PATH}")

    if blocked:
        sys.exit(1)


if __name__ == "__main__":
    main()
