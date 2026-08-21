#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全市場上市個股日資料：清洗 + 歷史累積機制（驗證階段，先跑「當日」一天）。

資料來源（TWSE 官方公開資料）：
  https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=csv
      全市場上市個股當日開高低收、成交量、成交值（含普通股、ETF、ETN、
      特別股、存託憑證(DR)、受益證券等所有類型，共用一組代號系統）。

      注意：這是同一份資料集在 www.twse.com.tw（網站用的即時資料）的版本，
      不是 openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL（官方 Open Data
      平台版本）。實測兩者發布時機不同：openapi.twse.com.tw 版本固定延遲一個
      完整交易日（例如週五下午查詢仍只回傳週四資料，不論當天多晚查都一樣），
      會導致每天發布的內容其實是「前一個交易日」的舊資料；這支 www.twse.com.tw
      版本收盤（13:30）後約2小時內即可查到當日資料，兩者欄位內容相同，只是
      發布時機不同，因此改用這支。

      為避免「TWSE 有回應但其實還是舊資料」被誤判成當日新資料，fetch_stock_day_all()
      額外核對回應內的日期是否等於台北時間今天——不是就當作「今日資料尚未發布」，
      跟 exit code 2（非交易日）走同一條路徑，不寫入任何資料、不發布任何內容。

清洗規則（只保留「普通股」）：
  先用代號規則判斷證券類型，規則是從實際資料的代號長度/前綴分布歸納出來，
  不是憑空猜測（見下方各 regex 旁註）：
    1. 名稱以 "-DR" 結尾                         -> 存託憑證(DR)，排除
    2. 代號符合 01xxxT（4碼含起始01 + 尾碼T）      -> 受益證券/REIT，排除
    3. 代號符合 02xxxx 或 02xxx+字母               -> ETN，排除
    4. 代號以 "00" 開頭（4~6碼皆有，如 0050、00xxx、00xxxA）-> ETF，排除
    5. 代號為 4碼數字 + 1~2 碼英數字尾碼，且名稱含「特」 -> 特別股，排除
    6. 代號恰好是 4 碼數字（且不以 00 開頭）        -> 普通股，保留
    7. 以上都不符合                                -> 無法分類，印出警告並排除（不臆測）

歷史存儲：
  data/stock_day_common.csv，欄位 Date,Code,Name,Close,Volume,Value。
  每次執行會先移除同一 Date 的舊資料再寫入（避免重跑造成重複列），
  然後只保留最近 30 個「交易日」（以 Date 唯一值計算，不是最近 30 列）。

若連不到 TWSE 明確回報被擋網域，不寫入任何假資料。
"""

import csv
import io
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

WWW_BASE = "https://www.twse.com.tw"
TIMEOUT = 30
RETRIES = 3
RETRY_DELAY_SEC = 2
MAX_TRADING_DAYS = 30
TAIPEI_TZ = ZoneInfo("Asia/Taipei")

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(REPO_ROOT, "data")
STORE_PATH = os.path.join(DATA_DIR, "stock_day_common.csv")

blocked_hosts = []
# TWSE 有回應、但資料是空的（非交易日/假日/尚未發布）——跟連線失敗是
# 兩件不同的事，呼叫端（daily_run.py）要能分開處理：連線失敗是真的錯誤，
# 空資料是「今天不用發布」的正常訊號，不該被當成失敗。
no_data_returned = False
# TWSE 有回應、資料格式也正常，但日期不是台北時間今天（今日資料尚未發布，
# 回應的還是上一個交易日的資料）——同樣走「今天不用發布」這條路徑，
# 不當成資料本身有誤。
stale_data_returned = False

CSV_FIELDNAME_MAP = {
    "日期": "Date",
    "證券代號": "Code",
    "證券名稱": "Name",
    "成交股數": "TradeVolume",
    "成交金額": "TradeValue",
    "收盤價": "ClosingPrice",
}


def _host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


def fetch_stock_day_all():
    global no_data_returned, stale_data_returned
    url = f"{WWW_BASE}/rwd/zh/afterTrading/STOCK_DAY_ALL"
    last_err = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(url, params={"response": "csv"}, timeout=TIMEOUT)
            resp.raise_for_status()
            reader = csv.DictReader(io.StringIO(resp.text))
            rows = list(reader)
            if not rows or "證券代號" not in (reader.fieldnames or []):
                print(f"[錯誤] {url} 回傳空資料，可能非交易日或尚未發布。", file=sys.stderr)
                no_data_returned = True
                return None
            records = [
                {new_key: row[old_key] for old_key, new_key in CSV_FIELDNAME_MAP.items()}
                for row in rows
            ]
            fetched_iso_date = roc_to_iso(records[0]["Date"])
            today_iso = datetime.now(TAIPEI_TZ).date().isoformat()
            if fetched_iso_date != today_iso:
                print(
                    f"[尚未發布] TWSE 目前回應的最新資料是 {fetched_iso_date}，"
                    f"不是台北時間今天（{today_iso}）：今日資料應是尚未發布，"
                    "不當作今日資料使用。",
                    file=sys.stderr,
                )
                stale_data_returned = True
                return None
            return records
        except requests.exceptions.RequestException as e:
            last_err = e
            if attempt < RETRIES - 1:
                time.sleep(RETRY_DELAY_SEC)
    host = _host(url)
    print(f"[錯誤] 無法連線至 {host}（{url}）：{last_err}", file=sys.stderr)
    blocked_hosts.append(host)
    return None


PREF_CODE_RE = re.compile(r"^\d{4}[A-Z0-9]{1,2}$")
BENEFICIARY_CODE_RE = re.compile(r"^01\d{3}T$")
ETN_CODE_RE = re.compile(r"^02\d{4}$|^02\d{3}[A-Z]$")
COMMON_CODE_RE = re.compile(r"^\d{4}$")


def classify(code: str, name: str) -> str:
    if name.endswith("-DR"):
        return "DR_存託憑證"
    if BENEFICIARY_CODE_RE.match(code):
        return "受益證券_REIT"
    if ETN_CODE_RE.match(code):
        return "ETN"
    if code.startswith("00"):
        return "ETF"
    if PREF_CODE_RE.match(code) and "特" in name:
        return "特別股"
    if COMMON_CODE_RE.match(code):
        return "普通股"
    return "無法分類"


def roc_to_iso(roc_date: str) -> str:
    year = int(roc_date[:3]) + 1911
    return f"{year}-{roc_date[3:5]}-{roc_date[5:7]}"


def to_number(s: str, as_int: bool = False):
    if s in (None, "", "--"):
        return None
    try:
        return int(s) if as_int else float(s)
    except ValueError:
        return None


def clean(records: list):
    buckets = {}
    for row in records:
        cat = classify(row["Code"], row["Name"])
        buckets.setdefault(cat, []).append(row)
    common = buckets.get("普通股", [])
    excluded = {k: v for k, v in buckets.items() if k != "普通股"}
    return common, excluded


def to_storage_rows(common_rows: list, iso_date: str):
    out = []
    for r in common_rows:
        out.append(
            {
                "Date": iso_date,
                "Code": r["Code"],
                "Name": r["Name"],
                "Close": to_number(r["ClosingPrice"]),
                "Volume": to_number(r["TradeVolume"], as_int=True),
                "Value": to_number(r["TradeValue"], as_int=True),
            }
        )
    return out


FIELDNAMES = ["Date", "Code", "Name", "Close", "Volume", "Value"]


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


def merge_and_trim(existing_rows: list, new_rows: list, iso_date: str):
    kept = [r for r in existing_rows if r["Date"] != iso_date]
    kept.extend({k: str(v) if v is not None else "" for k, v in r.items()} for r in new_rows)

    trading_days = sorted({r["Date"] for r in kept})
    keep_days = set(trading_days[-MAX_TRADING_DAYS:])
    trimmed = [r for r in kept if r["Date"] in keep_days]
    return trimmed


def main():
    records = fetch_stock_day_all()
    if not records:
        if (no_data_returned or stale_data_returned) and not blocked_hosts:
            # exit code 2：TWSE 有回應但資料是空的（非交易日/假日），或資料
            # 格式正常但日期不是台北時間今天（今日資料尚未發布）——兩種情況
            # 都跟連線失敗（exit 1）分開，讓呼叫端可以判斷「今天不用跑」而
            # 不是「今天跑失敗了」。
            reason = "TWSE 今日無資料（非交易日或假日）" if no_data_returned else "TWSE 今日資料尚未發布"
            print(f"[{reason}] 未寫入任何檔案。", file=sys.stderr)
            sys.exit(2)
        if blocked_hosts:
            print(f"[被擋網域] {sorted(set(blocked_hosts))}", file=sys.stderr)
        print("[失敗] 無法取得 STOCK_DAY_ALL 資料，未寫入任何檔案。", file=sys.stderr)
        sys.exit(1)

    iso_date = roc_to_iso(records[0]["Date"])
    total_before = len(records)

    common_rows, excluded = clean(records)

    print(f"日期：{iso_date}")
    print(f"清洗前總檔數：{total_before}")
    print(f"清洗後（僅普通股）：{len(common_rows)}")
    print()
    print("被排除類別與筆數：")
    for cat, rows in sorted(excluded.items(), key=lambda kv: -len(kv[1])):
        print(f"  {cat}: {len(rows)} 檔")
        for r in rows[:3]:
            print(f"    範例: {r['Code']}  {r['Name']}")
    if "無法分類" in excluded:
        print(
            "[警告] 有代號無法用現有規則分類，已列在上面「無法分類」中並排除，"
            "未強行歸類、未使用假資料。",
            file=sys.stderr,
        )

    storage_rows = to_storage_rows(common_rows, iso_date)
    existing_rows = load_existing()
    merged_rows = merge_and_trim(existing_rows, storage_rows, iso_date)
    write_store(merged_rows)

    trading_days_after = sorted({r["Date"] for r in merged_rows})
    print()
    print(f"寫入筆數（當日）：{len(storage_rows)}")
    print(f"存儲檔案路徑：{STORE_PATH}")
    print(f"存儲目前涵蓋交易日數：{len(trading_days_after)}（上限 {MAX_TRADING_DAYS}）")
    print(f"存儲目前總列數：{len(merged_rows)}")

    if blocked_hosts:
        print(f"[警告] 以下網域連線失敗：{sorted(set(blocked_hosts))}", file=sys.stderr)


if __name__ == "__main__":
    main()
