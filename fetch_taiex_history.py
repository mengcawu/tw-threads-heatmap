#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取台股大盤「發行量加權股價指數」歷史收盤指數，涵蓋 data/stock_day_common.csv
裡出現的每一個交易日，寫入 data/taiex.csv 供相對強度計算使用。

資料來源（TWSE 官方，逐日查詢，非估計/非造假）：
    https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=IND&response=json
每個日期取「價格指數(臺灣證券交易所)」表中「發行量加權股價指數」那一列的收盤指數。

注意：這個端點的 date 參數要用「西元」YYYYMMDD（例如 20260629），不是民國年；
若誤傳民國年格式（例如 1150629），伺服器不會報錯，而是靜默忽略、改回傳「今天」的
快照，且回應的 tables[].title 仍會用民國年顯示，非常容易誤判成資料正確。
保留「核對回應標題日期＋落地檢查點」的機制作為保險：每抓到一天就立刻寫入
data/.taiex_checkpoint.json，可跨多次執行續跑；全程核對回應標題日期，
不符或查無資料一律視為失敗、不採用，絕不用鄰近日期或估計值頂替。
"""

import csv
import json
import os
import sys
import time
from datetime import datetime

import requests

TIMEOUT = 15
WAVE_INTER_REQUEST_SLEEP = 0.5
WAVE_GAP_SLEEP = 3.0
MAX_WAVES = 100
CHECKPOINT_PATH = "data/.taiex_checkpoint.json"
OUTPUT_PATH = "data/taiex.csv"


def gregorian_date(iso_date: str) -> str:
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{dt.year}{dt.month:02d}{dt.day:02d}"


def roc_title_fragment(iso_date: str) -> str:
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{dt.year - 1911}年{dt.month:02d}月{dt.day:02d}日"


def try_fetch_once(iso_date: str):
    """單次嘗試。回傳 (close, None) 成功，或 (None, 錯誤訊息) 失敗。"""
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    params = {"date": gregorian_date(iso_date), "type": "IND", "response": "json"}
    expect_fragment = roc_title_fragment(iso_date)
    try:
        resp = requests.get(
            url, params=params, timeout=TIMEOUT, headers={"Connection": "close"}
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        return None, f"請求失敗：{e}"

    title_ok = False
    for table in data.get("tables", []):
        if not table.get("fields") or table["fields"][0] != "指數":
            continue
        title = table.get("title", "")
        if expect_fragment not in title:
            continue
        title_ok = True
        for row in table.get("data", []):
            if row[0] == "發行量加權股價指數":
                raw = row[1].replace(",", "").strip()
                if raw in ("", "--"):
                    return None, f"收盤指數為空值/無交易佔位符（{row[1]!r}）"
                try:
                    return float(raw), None
                except ValueError:
                    return None, f"收盤指數格式非預期：{row[1]!r}"
    if title_ok:
        return None, "表格標題日期相符，但找不到「發行量加權股價指數」列"
    return None, f"回應表格標題日期與查詢日期({expect_fragment})不符，疑似錯位快照"


def load_checkpoint():
    if os.path.exists(CHECKPOINT_PATH):
        with open(CHECKPOINT_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_existing_output():
    """讀已經存在的 data/taiex.csv 當初始快取。

    每日增量執行時，30天視窗裡通常只有「今天」是新的，其餘29天已經在
    上次執行時抓過、存在 data/taiex.csv 裡——沒有這層快取的話，每天都要
    重新抓整整30天，既浪費時間，也對這個已知會偶爾不穩定的歷史查詢端點
    增加不必要的請求次數。
    """
    if not os.path.exists(OUTPUT_PATH):
        return {}
    with open(OUTPUT_PATH, newline="", encoding="utf-8") as f:
        return {row["Date"]: float(row["TAIEX_Close"]) for row in csv.DictReader(f)}


def save_checkpoint(results):
    with open(CHECKPOINT_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


def main():
    with open("data/stock_day_common.csv", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        all_dates = sorted({row["Date"] for row in reader})

    results = load_existing_output()
    results.update(load_checkpoint())  # checkpoint 是較新的進度，蓋過舊快取
    print(
        f"目標 {len(all_dates)} 個交易日，既有資料/檢查點已有 {len(results)} 筆",
        file=sys.stderr,
        flush=True,
    )

    for wave in range(1, MAX_WAVES + 1):
        missing = [d for d in all_dates if d not in results]
        if not missing:
            break
        print(f"--- 第 {wave} 輪，剩餘 {len(missing)} 天 ---", file=sys.stderr, flush=True)
        for d in missing:
            close, err = try_fetch_once(d)
            if close is not None:
                results[d] = close
                save_checkpoint(results)
                print(f"  [OK] {d}: {close}", file=sys.stderr, flush=True)
            else:
                print(f"  [失敗] {d}: {err}", file=sys.stderr, flush=True)
            time.sleep(WAVE_INTER_REQUEST_SLEEP)
        missing_after = [d for d in all_dates if d not in results]
        if not missing_after:
            break
        time.sleep(WAVE_GAP_SLEEP)

    missing_final = [d for d in all_dates if d not in results]
    if missing_final:
        print(
            f"[未完成] 仍缺 {len(missing_final)} 天：{missing_final}，"
            "檢查點已保留，之後可重新執行本腳本續跑。",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

    rows = [{"Date": d, "TAIEX_Close": results[d]} for d in all_dates]
    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Date", "TAIEX_Close"])
        writer.writeheader()
        writer.writerows(rows)
    os.remove(CHECKPOINT_PATH)
    print(f"完成，共 {len(rows)} 個交易日寫入 {OUTPUT_PATH}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
