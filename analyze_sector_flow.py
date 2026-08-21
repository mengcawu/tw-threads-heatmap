#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
法人資金流向－產業類別分析：把「法人淨買超金額」依 TWSE 官方產業別分類加總，
排出資金流向前幾大產業類別，並列出各產業內淨買超金額最高的幾檔個股
（「受資金青睞」＝該產業內淨買超金額最高者）。

「法人淨買超金額」定義：data/institutional.csv 當日 Total（三大法人合計買賣超
股數）× data/stock_day_common.csv 當日 Close（收盤價），逐檔計算。這是市場
慣用的近似算法——TWSE 沒有公開「個股層級」的法人買賣金額（只公開股數），
用收盤價估算金額是通用做法，但不是法人實際成交均價，此為已知誤差來源，
在 output JSON 的 notes 裡明記。

產業分類資料來源（TWSE 官方公開資料，跟 fetch.py 共用同一套邏輯/函式）：
  https://openapi.twse.com.tw/v1/opendata/t187ap03_L   公司代號 -> 產業別代碼
  https://openapi.twse.com.tw/v1/opendata/t187ap14_L   公司代號 -> 產業別中文名稱
                                                         （EPS統計資料，join出代碼->名稱）
只涵蓋已對照到中文名稱的產業別代碼；對應不到的個股直接排除、不臆測產業別。

排名規則：
  1. 只計入當日 Total > 0（淨買超）的個股，依產業別加總，只保留加總為正的
     產業（負值代表該產業當日整體是法人淨賣超，不算「資金流向」進去）。
  2. 產業依加總淨買超金額由大到小排序，取前 TOP_N_SECTORS 大；不足就全部
     列出、不補齊。
  3. 每個入選產業內，個股依淨買超金額由大到小排序，取前 TOP_N_STOCKS_PER_SECTOR
     檔（同樣只算淨買超 > 0 的個股）。

輸出：output/sector_flow.json（找不到對應日期的 institutional.csv 資料時，
仍輸出空 sectors 陣列，不報錯中止——呼叫端 generate_caption.py 自行決定
要不要省略這段文案）。
"""

import csv
import json
import sys
from pathlib import Path

import requests

import fetch as fetch_module

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
OUTPUT_DIR = REPO_ROOT / "output"
PRICE_PATH = DATA_DIR / "stock_day_common.csv"
INST_PATH = DATA_DIR / "institutional.csv"
OUTPUT_JSON = OUTPUT_DIR / "sector_flow.json"

TOP_N_SECTORS = 3
TOP_N_STOCKS_PER_SECTOR = 3


def load_latest_date(path: Path) -> str:
    with path.open(newline="", encoding="utf-8") as f:
        dates = {row["Date"] for row in csv.DictReader(f)}
    return max(dates) if dates else None


def load_close_by_code(date_str: str) -> dict:
    with PRICE_PATH.open(newline="", encoding="utf-8") as f:
        return {
            row["Code"]: float(row["Close"])
            for row in csv.DictReader(f)
            if row["Date"] == date_str and row["Close"] not in (None, "")
        }


def load_institutional_by_code(date_str: str) -> dict:
    with INST_PATH.open(newline="", encoding="utf-8") as f:
        return {
            row["Code"]: (row["Name"], float(row["Total"]))
            for row in csv.DictReader(f)
            if row["Date"] == date_str and row["Total"] not in (None, "")
        }


def main():
    price_date = load_latest_date(PRICE_PATH)
    inst_date = load_latest_date(INST_PATH)

    result = {
        "report_date": None,
        "sectors": [],
        "notes": [
            "「法人淨買超金額」= data/institutional.csv 當日三大法人合計買賣超股數 "
            "× data/stock_day_common.csv 當日收盤價，市場慣用近似算法，"
            "非法人實際成交均價（TWSE 未公開個股層級法人買賣金額）。",
            "只計入當日淨買超為正的個股與產業別加總為正的產業，負值（淨賣超）不列入。",
            "產業分類依 TWSE 官方 t187ap03_L／t187ap14_L 開放資料，對應不到中文名稱的"
            "個股或產業別代碼直接排除，不臆測。",
        ],
    }

    if inst_date is None or price_date != inst_date:
        print(
            f"[警告] data/stock_day_common.csv 最新日期（{price_date}）與 "
            f"data/institutional.csv 最新日期（{inst_date}）不一致，"
            "今日法人資料可能尚未發布，輸出空的產業資金流向。",
            file=sys.stderr,
        )
        OUTPUT_DIR.mkdir(exist_ok=True)
        OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    result["report_date"] = price_date
    close_by_code = load_close_by_code(price_date)
    inst_by_code = load_institutional_by_code(inst_date)

    company_to_code = fetch_module.get_company_industry_codes()
    code_to_name = fetch_module.get_industry_code_names(company_to_code)
    if not company_to_code or not code_to_name:
        print(
            "[警告] 無法取得 TWSE 產業別分類資料（t187ap03_L／t187ap14_L），"
            "輸出空的產業資金流向。",
            file=sys.stderr,
        )
        OUTPUT_DIR.mkdir(exist_ok=True)
        OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # 逐檔計算淨買超金額，只留淨買超 > 0，且能對應到產業中文名稱的個股。
    stock_flows = []  # [(sector_label, code, name, value)]
    for code, (name, net_shares) in inst_by_code.items():
        if net_shares <= 0:
            continue
        close = close_by_code.get(code)
        if close is None:
            continue
        industry_code = company_to_code.get(code)
        if not industry_code:
            continue
        sector_name = code_to_name.get(industry_code)
        if not sector_name:
            continue
        sector_label = fetch_module.normalize_sector_label(sector_name)
        value = net_shares * close
        stock_flows.append((sector_label, code, name, value))

    sector_totals = {}
    for sector_label, _, _, value in stock_flows:
        sector_totals[sector_label] = sector_totals.get(sector_label, 0) + value

    top_sectors = sorted(
        (item for item in sector_totals.items() if item[1] > 0),
        key=lambda item: item[1],
        reverse=True,
    )[:TOP_N_SECTORS]

    for sector_label, total_value in top_sectors:
        stocks_in_sector = sorted(
            (f for f in stock_flows if f[0] == sector_label),
            key=lambda f: f[3],
            reverse=True,
        )[:TOP_N_STOCKS_PER_SECTOR]
        result["sectors"].append(
            {
                "sector": sector_label,
                "net_buy_value_twd": round(total_value, 0),
                "stocks": [
                    {"code": code, "name": name, "net_buy_value_twd": round(value, 0)}
                    for _, code, name, value in stocks_in_sector
                ],
            }
        )

    OUTPUT_DIR.mkdir(exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if fetch_module.blocked_hosts:
        print(
            f"[警告] 以下網域連線失敗：{sorted(set(fetch_module.blocked_hosts))}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
