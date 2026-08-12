#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回補：三大法人個股買賣超，對齊 data/stock_day_common.csv 的交易日範圍。

資料來源（TWSE 官方公開資料，逐日回補）：
  https://www.twse.com.tw/rwd/zh/fund/T86?date=YYYYMMDD&selectType=ALLBUT0999&response=json
      三大法人買賣超日報，個股別，接受 date 參數（已驗證可取歷史，非僅當日快照）。
      欄位：外陸資買賣超股數(不含外資自營商) + 外資自營商買賣超股數 = 外資買賣超，
            投信買賣超股數 = 投信買賣超，自營商買賣超股數 = 自營商買賣超（已含自行買賣+避險），
            三大法人買賣超股數 = 合計（外資+投信+自營商，已於樣本核對一致）。

清洗規則：與 ingest_stock_day.py 完全相同，重用其 classify()，只保留「普通股」。

對齊策略：不獨立往回試探交易日，而是直接沿用 data/stock_day_common.csv 目前
已回補好的交易日清單（Date 唯一值）逐一查詢，確保 Code+Date 能與量價資料對上。
若某個已知交易日在 T86 查不到資料（stat!=OK），如實記錄為缺漏，不補假資料。

存儲：data/institutional.csv，欄位
  Date,Code,Name,ForeignNet,TrustNet,DealerNet,Total
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
STORE_PATH = os.path.join(DATA_DIR, "institutional.csv")
FIELDNAMES = ["Date", "Code", "Name", "ForeignNet", "TrustNet", "DealerNet", "Total"]


def fetch_t86_day(date_str: str):
    url = f"{WWW_BASE}/rwd/zh/fund/T86"
    params = {"date": date_str, "selectType": "ALLBUT0999", "response": "json"}
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


def extract_t86_rows(data: dict):
    fields = data.get("fields") or []
    try:
        idx_code = fields.index("證券代號")
        idx_name = fields.index("證券名稱")
        idx_foreign_ex_dealer = fields.index("外陸資買賣超股數(不含外資自營商)")
        idx_foreign_dealer = fields.index("外資自營商買賣超股數")
        idx_trust = fields.index("投信買賣超股數")
        idx_dealer = fields.index("自營商買賣超股數")
        idx_total = fields.index("三大法人買賣超股數")
    except ValueError:
        return None
    rows = []
    for row in data.get("data") or []:
        def num(s):
            s = s.replace(",", "").strip()
            return int(s) if s not in ("", "--") else 0

        foreign_net = num(row[idx_foreign_ex_dealer]) + num(row[idx_foreign_dealer])
        rows.append(
            {
                "Code": row[idx_code],
                "Name": row[idx_name].strip(),
                "ForeignNet": foreign_net,
                "TrustNet": num(row[idx_trust]),
                "DealerNet": num(row[idx_dealer]),
                "Total": num(row[idx_total]),
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
    print(f"回補前 institutional.csv 已有交易日數：{len(existing_days)}")

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
        data = fetch_t86_day(date_str)
        time.sleep(REQUEST_DELAY_SEC + random.uniform(0, 0.5))

        if data is None:
            blocked = True
            break

        if data.get("stat") != "OK":
            print(f"[缺漏] {iso}：T86 回傳 stat={data.get('stat')!r}，該日無三大法人資料。", file=sys.stderr)
            missing_days.append(iso)
            continue

        raw_rows = extract_t86_rows(data)
        if not raw_rows:
            print(f"[警告] {iso} stat=OK 但欄位解析失敗，略過。", file=sys.stderr)
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
                "ForeignNet": r["ForeignNet"],
                "TrustNet": r["TrustNet"],
                "DealerNet": r["DealerNet"],
                "Total": r["Total"],
            }
            for r in common_rows
        ]

        all_rows = merge_for_date(all_rows, new_rows, iso)
        fetched_days.append(iso)
        print(f"[完成] {iso}：{len(new_rows)} 檔普通股（{len(fetched_days)}/{len(price_days)}）")

    # 裁剪到跟 stock_day_common.csv 完全一樣的交易日視窗（滾動30天）：
    # price_days 已經是 ingest_stock_day.py 裁剪過的最新30天，這裡把落在這個
    # 視窗之外的舊資料一併丟掉，institutional.csv 才不會隨著每天執行無限累積。
    price_days_set = set(price_days)
    dropped = len({r["Date"] for r in all_rows} - price_days_set)
    all_rows = [r for r in all_rows if r["Date"] in price_days_set]
    if dropped:
        print(f"[裁剪] 移除 {dropped} 個超出目前30天視窗的舊交易日。", file=sys.stderr)

    write_store(all_rows)

    stored_days = sorted({r["Date"] for r in all_rows})
    print()
    print("========== institutional.csv 回補結果 ==========")
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
        print(f"[缺漏] 以下 {len(missing_days)} 個交易日 T86 無資料，未補假資料：{missing_days}")
    print(f"存儲檔案路徑：{STORE_PATH}")

    if blocked:
        sys.exit(1)


if __name__ == "__main__":
    main()
