#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
歷史回補：把 data/stock_day_common.csv 填到最近 30 個交易日。

背景：ingest_stock_day.py 用的 STOCK_DAY_ALL
（https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL）已確認只回傳
「當日」快照，不接受日期參數，無法拿來回補歷史。

改用的官方替代來源（同樣是 TWSE 官方公開資料，逐日回補）：
  https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=ALLBUT0999&response=json
      接受 date 參數，回傳當天「每日收盤行情(全部，不含權證/牛熊證/可展延牛熊證)」表格，
      涵蓋範圍與 STOCK_DAY_ALL 相同（普通股、ETF、ETN、特別股、DR、受益證券等），
      欄位對應：證券代號->Code、證券名稱->Name、收盤價->ClosingPrice、
      成交股數->TradeVolume、成交金額->TradeValue（數字皆為含千分位逗號字串，回補時去除）。
      非交易日（假日/週末）該 API 回傳 stat != "OK" 且無資料表格，視為略過。

清洗規則與歷史存儲的合併/裁切邏輯，直接重用 ingest_stock_day.py 裡已驗證過的
classify() / clean() / to_storage_rows() / merge_and_trim() / write_store()，
不重複實作，確保跟「當日」流程完全一致。

流程：
  以目前 CSV 裡最早的交易日為錨點，往回逐日（含週末/假日）呼叫 MI_INDEX，
  每次請求間加上延遲避免限流；stat!=OK 視為非交易日直接跳過；
  蒐集到的交易日數 + CSV 既有交易日數達到 30 天即停止，
  或往回超過安全上限（MAX_CALENDAR_LOOKBACK 個日曆天）仍不足也停止並如實回報。

若途中連線被擋或疑似被限流（重試後仍失敗），立刻停止回補、印出實際涵蓋範圍，
不會用假資料湊滿 30 天。
"""

import random
import sys
import time
from datetime import date, timedelta

import requests

import ingest_stock_day as base

WWW_BASE = "https://www.twse.com.tw"
TIMEOUT = 20
RETRIES = 3
REQUEST_DELAY_SEC = 1.5  # 每次請求間隔基準秒數，避免對 TWSE 造成短時間高頻請求
MAX_CALENDAR_LOOKBACK = 60  # 往回查詢的日曆天數上限（安全閥，非交易日很多時避免無窮迴圈）


def fetch_mi_index_day(date_str: str):
    """回傳當天 MI_INDEX JSON，或在連線失敗（重試後）時回傳 None 並記錄被擋網域。"""
    url = f"{WWW_BASE}/rwd/zh/afterTrading/MI_INDEX"
    params = {"date": date_str, "type": "ALLBUT0999", "response": "json"}
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


def extract_stock_rows(mi_data: dict):
    for t in mi_data.get("tables", []):
        title = t.get("title") or ""
        if "每日收盤行情" in title:
            fields = t.get("fields") or []
            try:
                idx_code = fields.index("證券代號")
                idx_name = fields.index("證券名稱")
                idx_close = fields.index("收盤價")
                idx_volume = fields.index("成交股數")
                idx_value = fields.index("成交金額")
            except ValueError:
                return None
            rows = []
            for row in t.get("data") or []:
                rows.append(
                    {
                        "Code": row[idx_code],
                        "Name": row[idx_name],
                        "ClosingPrice": row[idx_close].replace(",", ""),
                        "TradeVolume": row[idx_volume].replace(",", ""),
                        "TradeValue": row[idx_value].replace(",", ""),
                    }
                )
            return rows
    return None


def continuity_check(all_rows: list, trading_days: list):
    codes = sorted({r["Code"] for r in all_rows})
    if not codes:
        print("（存儲為空，無法抽樣）")
        return
    sample_code = random.choice(codes)
    code_rows = [r for r in all_rows if r["Code"] == sample_code]
    code_dates = sorted(r["Date"] for r in code_rows)
    name = code_rows[0]["Name"] if code_rows else ""
    missing = [d for d in trading_days if d not in code_dates]
    print(f"隨機抽樣個股：{sample_code} {name}")
    print(f"  出現交易日數：{len(code_dates)} / {len(trading_days)}")
    if missing:
        print(f"  缺少的交易日（序列不連續）：{missing}")
    else:
        print("  30 日序列連續，無缺漏。")


def main():
    existing_rows = base.load_existing()
    existing_days = sorted({r["Date"] for r in existing_rows})
    target_total = base.MAX_TRADING_DAYS

    print(f"回補前已有交易日數：{len(existing_days)}")
    if existing_days:
        print(f"回補前日期範圍：{existing_days[0]} ~ {existing_days[-1]}")

    if len(existing_days) >= target_total:
        print(f"已達 {target_total} 個交易日上限，無需回補。")
        all_rows = existing_rows
        blocked = False
        calendar_checked = 0
        skipped_non_trading = []
        newly_added_days = []
    else:
        anchor = (
            date.fromisoformat(existing_days[0]) - timedelta(days=1)
            if existing_days
            else date.today() - timedelta(days=1)
        )

        have_days = set(existing_days)
        all_rows = list(existing_rows)
        newly_added_days = []
        skipped_non_trading = []
        excluded_totals = {}
        calendar_checked = 0
        blocked = False

        d = anchor
        while len(have_days) < target_total and calendar_checked < MAX_CALENDAR_LOOKBACK:
            iso = d.isoformat()
            date_str = d.strftime("%Y%m%d")
            calendar_checked += 1

            if iso in have_days:
                d -= timedelta(days=1)
                continue

            mi_data = fetch_mi_index_day(date_str)
            time.sleep(REQUEST_DELAY_SEC + random.uniform(0, 0.5))

            if mi_data is None:
                blocked = True
                break

            if mi_data.get("stat") != "OK":
                skipped_non_trading.append(iso)
                d -= timedelta(days=1)
                continue

            raw_rows = extract_stock_rows(mi_data)
            if not raw_rows:
                print(f"[警告] {iso} stat=OK 但找不到「每日收盤行情」表格，略過。", file=sys.stderr)
                d -= timedelta(days=1)
                continue

            common_rows, excluded = base.clean(raw_rows)
            for cat, rows in excluded.items():
                excluded_totals[cat] = excluded_totals.get(cat, 0) + len(rows)
            if "無法分類" in excluded:
                for r in excluded["無法分類"][:3]:
                    print(f"[警告] {iso} 無法分類代號範例：{r['Code']} {r['Name']}", file=sys.stderr)

            storage_rows = base.to_storage_rows(common_rows, iso)
            all_rows = base.merge_and_trim(all_rows, storage_rows, iso)
            have_days.add(iso)
            newly_added_days.append(iso)
            print(f"[完成] {iso}：{len(storage_rows)} 檔普通股（累計交易日 {len(have_days)}/{target_total}）")

            d -= timedelta(days=1)

        if excluded_totals:
            print()
            print("回補期間被排除類別加總：")
            for cat, n in sorted(excluded_totals.items(), key=lambda kv: -kv[1]):
                print(f"  {cat}: {n} 檔次")

    base.write_store(all_rows)

    trading_days_after = sorted({r["Date"] for r in all_rows})
    print()
    print("========== 回補結果 ==========")
    if blocked:
        print(f"[中止] 連線被擋或重試後仍失敗，回補中途停止：{sorted(set(base.blocked_hosts))}", file=sys.stderr)
        print("[中止] 以下為停止前實際已寫入的結果，未使用任何假資料補齊。", file=sys.stderr)
    print(f"新增交易日數：{len(newly_added_days)}（{newly_added_days if newly_added_days else '無'}）")
    print(f"略過的非交易日（日曆天，共 {len(skipped_non_trading)} 天）：{skipped_non_trading}")
    print(f"實際查詢的日曆天數：{calendar_checked}（上限 {MAX_CALENDAR_LOOKBACK}）")
    print(f"存儲目前涵蓋交易日數：{len(trading_days_after)}（目標 {target_total}）")
    if trading_days_after:
        print(f"存儲目前日期範圍：{trading_days_after[0]} ~ {trading_days_after[-1]}")
    print(f"存儲目前總列數：{len(all_rows)}")
    print(f"存儲檔案路徑：{base.STORE_PATH}")
    print()
    continuity_check(all_rows, trading_days_after)

    if blocked:
        sys.exit(1)
    if len(trading_days_after) < target_total:
        print(
            f"[提示] 已查完 {calendar_checked} 個日曆天仍只湊到 {len(trading_days_after)} 個交易日"
            f"（未達 {target_total}），可能是市場休市天數較多或資料尚未發布，未強行湊數。",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
