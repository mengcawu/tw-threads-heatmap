#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
帶量突破訊號掃描（近30交易日全市場普通股）。

指標定義：
  20日均量  = 當日之前 20 個交易日成交量(股數)平均，計算時排除 Close 為空/無成交的日子
  量能倍數  = 當日成交量 ÷ 20日均量
  20日新高  = 當日收盤 ≥ 該檔「當日之前」20 個交易日收盤最高（排除空值日）
  帶量突破訊號 = 當日收盤創20日新高 且 量能倍數 ≥ 1.5
  連續新高天數 = 從當日往前，連續幾個交易日收盤都創其「當日」的20日新高
                （每一天各自用該日之前20個有效交易日判斷，非固定視窗）

雜訊過濾（達標後才納入榜單）：
  剔除當日成交值 < 50,000,000 (TradeValue)
  剔除當日無成交/停牌 (Close 空)

資料不足：某檔股票在「最新交易日」之前，若有效交易日（Close 非空）不足 20 天
（即無法湊齊 20 天歷史 + 當日，共需至少 21 個有效交易日），則跳過該檔，
並計入「資料不足被跳過」計數。
"""

import csv
import json
import os
from collections import defaultdict

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(REPO_ROOT, "data", "stock_day_common.csv")

WINDOW = 20
VOLUME_MULT_THRESHOLD = 1.5
MIN_TRADE_VALUE = 50_000_000


def load_rows():
    with open(DATA_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def group_by_code(rows):
    by_code = defaultdict(list)
    for r in rows:
        by_code[r["Code"]].append(r)
    for code, recs in by_code.items():
        recs.sort(key=lambda r: r["Date"])
    return by_code


def valid_only(recs):
    """只保留 Close 非空（有成交）的交易日，依日期由舊到新。"""
    out = []
    for r in recs:
        close = r["Close"]
        if close is None or close.strip() == "":
            continue
        out.append(
            {
                "Date": r["Date"],
                "Name": r["Name"],
                "Close": float(close),
                "Volume": float(r["Volume"]) if r["Volume"].strip() else 0.0,
                "Value": float(r["Value"]) if r["Value"].strip() else 0.0,
            }
        )
    return out


def is_new_high_at(v, i):
    """v[i] 是否為其「當日之前」20個有效交易日的新高。i 需 >= WINDOW。"""
    window = v[i - WINDOW : i]
    hi = max(x["Close"] for x in window)
    return v[i]["Close"] >= hi


def volume_multiple_at(v, i):
    window = v[i - WINDOW : i]
    avg_vol = sum(x["Volume"] for x in window) / WINDOW
    if avg_vol <= 0:
        return None
    return v[i]["Volume"] / avg_vol


def consecutive_new_high_days(v, i):
    """從 i 往前數，連續幾天都滿足「當日創其20日新高」；資料不足處視為中止。"""
    streak = 0
    j = i
    while j >= WINDOW:
        if is_new_high_at(v, j):
            streak += 1
            j -= 1
        else:
            break
    return streak


def main():
    rows = load_rows()
    all_dates = sorted(set(r["Date"] for r in rows))
    latest_date = all_dates[-1]

    by_code = group_by_code(rows)

    skipped_insufficient = 0
    skipped_noise = 0
    hits = []

    for code, recs in by_code.items():
        v = valid_only(recs)
        if not v or v[-1]["Date"] != latest_date:
            # 當日無成交/停牌，或最新一天資料缺失
            continue

        i = len(v) - 1  # 當日索引

        if i < WINDOW:
            skipped_insufficient += 1
            continue

        today = v[i]
        prev = v[i - 1]

        if today["Value"] < MIN_TRADE_VALUE:
            skipped_noise += 1
            continue

        vol_mult = volume_multiple_at(v, i)
        if vol_mult is None:
            skipped_insufficient += 1
            continue

        new_high = is_new_high_at(v, i)
        breakout = new_high and vol_mult >= VOLUME_MULT_THRESHOLD

        if not breakout:
            continue

        pct_change = (today["Close"] - prev["Close"]) / prev["Close"] * 100
        streak = consecutive_new_high_days(v, i)

        hits.append(
            {
                "code": code,
                "name": today["Name"],
                "close": today["Close"],
                "pct_change": round(pct_change, 2),
                "volume_multiple": round(vol_mult, 1),
                "consecutive_new_high_days": streak,
                "trade_value": int(today["Value"]),
            }
        )

    hits.sort(key=lambda h: h["volume_multiple"], reverse=True)

    result = {
        "date": latest_date,
        "market_hit_count": len(hits),
        "skipped_insufficient_history": skipped_insufficient,
        "skipped_noise_filter": skipped_noise,
        "signals": hits,
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
