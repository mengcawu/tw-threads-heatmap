#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
優質股過濾 + 四維吸籌評分。

輸入（近 30 個交易日，已對齊）：
    data/stock_day_common.csv  Date,Code,Name,Close,Volume,Value
    data/institutional.csv     Date,Code,Name,ForeignNet,TrustNet,DealerNet,Total
    data/margin.csv             Date,Code,Name,MarginBalance,MarginChange,ShortBalance
    data/taiex.csv               Date,TAIEX_Close（大盤發行量加權股價指數，TWSE 官方逐日查詢）

輸出：
    stdout 印出完整 JSON 榜單（前 TOP_N 檔），供人工檢驗。

所有數字皆來自上述 CSV 的實際資料，不做任何估計/造假；缺資料一律照下方
「缺漏處理」規則處理，不使用臆測值。
"""

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime

# ============================================================
# 常數：門檻與權重（之後要調整只改這裡）
# ============================================================

# ---- 第一層：優質股門檻 ----
LOOKBACK_DAYS = 20  # "近20交易日" 的視窗長度
MIN_AVG_VALUE_20D = 200_000_000  # 近20日平均成交值 ≥ 2億（新台幣）
MARKET_CAP_THRESHOLD = 10_000_000_000  # 市值 ≥ 100億（本資料集無市值欄位，未使用，僅供對照）
# 無市值資料，改用「近20日平均成交值 ≥ 5億」作為市值門檻的代理（proxy）
PROXY_MIN_AVG_VALUE_20D = 500_000_000
# 無全額交割/處置/注意股標記資料，此條件略過（見下方 NOTES）

# ---- 第二層：四維吸籌評分權重（合計須為 1.0）----
WEIGHT_INSTITUTIONAL_STREAK = 0.35  # 維度1：法人連續買超
WEIGHT_BUY_WITHOUT_RETAIL = 0.30  # 維度2：法人買、散戶（融資）沒跟
WEIGHT_MILD_VOLUME_UPTREND = 0.20  # 維度3：價漲量溫和（爆量扣分）
WEIGHT_RELATIVE_STRENGTH = 0.15  # 維度4：相對大盤強度
assert abs(
    WEIGHT_INSTITUTIONAL_STREAK
    + WEIGHT_BUY_WITHOUT_RETAIL
    + WEIGHT_MILD_VOLUME_UPTREND
    + WEIGHT_RELATIVE_STRENGTH
    - 1.0
) < 1e-9

NEUTRAL_SCORE = 50  # 資料缺漏時該維度給的中性分

RECENT_WINDOW = 5  # "近5日" 視窗長度（維度2、維度3使用）

# 維度1：連續買超天數 -> 分數 對照表（外資/投信取較高者）
STREAK_SCORE_MAP = {0: 0, 1: 40, 2: 60, 3: 80}
STREAK_SCORE_MAX_DAYS = 4  # 連4日以上 = 100
STREAK_SCORE_CAP = 100

# 維度2：法人買、散戶沒跟——融資變化相對法人買超力道的比例 -> 分數 的分段線性映射
#   ratio = (近5日融資餘額變化，換算成張) / max(近5日法人合計買超換算成張, 1)
#   ratio <= 0（融資打平或減少）           -> 100 分
#   0 < ratio <= 1（融資同步增加，最多追平） -> 100 線性降到 0 分
#   ratio > 1（融資增加超過法人買超力道）    -> 0 分
#   法人近5日合計買超 <= 0（法人本身沒買）  -> 直接給低分（不符合此維度定義的吸籌型態）
BUY_WITHOUT_RETAIL_NO_INST_BUY_SCORE = 20
SHARES_PER_LOT = 1000  # 1張 = 1000股，institutional.csv 是「股」，margin.csv 是「張」

# 維度3：量能倍數（今日成交量 / 近5日(不含今日)均量）分段評分
VOLUME_RATIO_LOOKBACK = 5  # 量能倍數的均量基準天數（不含當日）
VOL_HIGH_BAND = (1.0, 1.8)  # 落在此區間 -> 100分（若同時近5日收盤走高）
VOL_EXTREME_HIGH = 2.5  # 超過此倍數 -> 低分（爆量扣分）
VOL_LOW_CUT = 0.8  # 低於此倍數 -> 低分（量縮）
VOL_SCORE_EXTREME_HIGH = 10
VOL_SCORE_LOW = 20
VOL_SCORE_BELOW_BAND_MIN = 30  # ratio 在 [0.8,1.0) 區間的下限分數
VOL_SCORE_BELOW_BAND_MAX = 60  # ratio 在 [0.8,1.0) 區間的上限分數（接近1.0時）
VOL_SCORE_ABOVE_BAND_FLOOR = 20  # ratio 在 (1.8,2.5] 區間的下限分數（接近2.5時）
NO_UPTREND_SCORE_CAP = 40  # 近5日收盤未走高時，維度3分數上限（不管量能多理想）

# 維度4：相對強度（個股近20日漲幅 - 大盤近20日漲幅，百分點）-> 分數
# score = clip(50 + rs_pct * RS_SCALE, 0, 100)；RS_SCALE 代表每 1 個百分點相對強度對應的分數
RS_SCALE = 2.5  # 相對大盤 +20 個百分點 -> 滿分100；-20個百分點 -> 0分

TOP_N = 15

NOTES = [
    "全額交割/處置/注意股標記：本資料集（data/stock_day_common.csv 等）未包含此標記欄位，"
    "第一層門檻中「排除全額交割/處置/注意股」這一條略過未執行。",
    "市值門檻：本資料集無市值欄位，改用「近20日平均成交值 ≥ 5億」作為市值≥100億門檻的代理（proxy）。",
    "大盤近20日漲幅：data/taiex.csv 為 TWSE 官方「發行量加權股價指數」逐日收盤指數，"
    "涵蓋 data/stock_day_common.csv 出現的全部30個交易日，非估計值。",
    "量能倍數定義：當日成交量 / 近5個交易日（不含當日）平均成交量。",
    "近5日收盤走高定義：收盤價(最新日) > 收盤價(5個交易日前)，即近5日報酬率為正。",
]


def load_price_series():
    series = defaultdict(list)  # code -> [ (date, close, volume, value) ] sorted by date
    names = {}
    with open("data/stock_day_common.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            code = row["Code"]
            names[code] = row["Name"]
            # 少數個股當日無成交，Close 欄位為空字串；該日跳過不計入該股票的價量序列
            # （這些個股成交量/值都極小，遠低於門檻，不影響優質股過濾結果）。
            if row["Close"] == "" or row["Volume"] == "" or row["Value"] == "":
                continue
            series[code].append(
                (
                    row["Date"],
                    float(row["Close"]),
                    float(row["Volume"]),
                    float(row["Value"]),
                )
            )
    for code in series:
        series[code].sort(key=lambda r: r[0])
    return series, names


def load_institutional_series():
    series = defaultdict(dict)  # code -> {date: (foreign, trust, dealer, total)}
    with open("data/institutional.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            series[row["Code"]][row["Date"]] = (
                float(row["ForeignNet"]),
                float(row["TrustNet"]),
                float(row["DealerNet"]),
                float(row["Total"]),
            )
    return series


def load_margin_series():
    series = defaultdict(dict)  # code -> {date: (balance, change, short)}
    with open("data/margin.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            series[row["Code"]][row["Date"]] = (
                float(row["MarginBalance"]),
                float(row["MarginChange"]),
                float(row["ShortBalance"]),
            )
    return series


def load_taiex_series():
    rows = []
    with open("data/taiex.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append((row["Date"], float(row["TAIEX_Close"])))
    rows.sort(key=lambda r: r[0])
    return rows


# ============================================================
# 第一層：優質股門檻
# ============================================================


def passes_quality_gate(price_rows):
    """price_rows: 該股票依日期排序的 (date, close, volume, value) 列表。"""
    recent = price_rows[-LOOKBACK_DAYS:]
    if len(recent) < LOOKBACK_DAYS:
        return False, None, None
    avg_value_20d = sum(r[3] for r in recent) / len(recent)
    # 兩條門檻分開檢查、各自可調整：以目前的常數值（2億 vs 代理用的5億）來說，
    # 代理門檻較嚴，實際會是拘束條件；若之後調鬆 PROXY_MIN_AVG_VALUE_20D（例如改用
    # 真實市值資料取代代理），MIN_AVG_VALUE_20D 這條「近20日均成交值≥2億」才會重新生效。
    passes = avg_value_20d >= MIN_AVG_VALUE_20D and avg_value_20d >= PROXY_MIN_AVG_VALUE_20D
    return passes, avg_value_20d, recent


# ============================================================
# 第二層：四維評分
# ============================================================


def score_institutional_streak(code, dates_20d, inst_series):
    """維度1：外資或投信「連續買超天數」，取較高者轉分數。"""
    inst_by_date = inst_series.get(code)
    if not inst_by_date:
        return NEUTRAL_SCORE, "無法人買賣超資料"

    def streak(idx):  # idx: 0=ForeignNet, 1=TrustNet
        count = 0
        for d in reversed(dates_20d):
            rec = inst_by_date.get(d)
            if rec is None:
                break
            if rec[idx] > 0:
                count += 1
            else:
                break
        return count

    foreign_streak = streak(0)
    trust_streak = streak(1)

    def streak_to_score(n):
        if n >= STREAK_SCORE_MAX_DAYS:
            return STREAK_SCORE_CAP
        return STREAK_SCORE_MAP.get(n, 0)

    foreign_score = streak_to_score(foreign_streak)
    trust_score = streak_to_score(trust_streak)

    if foreign_score >= trust_score:
        fact = f"外資連{foreign_streak}日買超" if foreign_streak > 0 else "外資未連續買超"
    else:
        fact = f"投信連{trust_streak}日買超" if trust_streak > 0 else "投信未連續買超"

    return max(foreign_score, trust_score), fact


def score_buy_without_retail(code, dates_20d, inst_series, margin_series):
    """維度2：近5日法人合計買超為正，且同期融資餘額打平/減少 -> 高分。"""
    inst_by_date = inst_series.get(code)
    margin_by_date = margin_series.get(code)
    if not inst_by_date or not margin_by_date:
        missing = []
        if not inst_by_date:
            missing.append("法人")
        if not margin_by_date:
            missing.append("融資")
        return NEUTRAL_SCORE, f"缺{('、'.join(missing))}資料"

    recent_dates = dates_20d[-RECENT_WINDOW:]
    inst_recs = [inst_by_date.get(d) for d in recent_dates]
    margin_recs = [margin_by_date.get(d) for d in recent_dates]
    if any(r is None for r in inst_recs) or any(r is None for r in margin_recs):
        return NEUTRAL_SCORE, "近5日法人或融資資料不完整"

    inst_sum_5d_shares = sum(r[3] for r in inst_recs)  # Total 欄位（股）
    inst_sum_5d_lots = inst_sum_5d_shares / SHARES_PER_LOT
    margin_chg_5d_lots = sum(r[1] for r in margin_recs)  # MarginChange 欄位（張）

    if inst_sum_5d_lots <= 0:
        return (
            BUY_WITHOUT_RETAIL_NO_INST_BUY_SCORE,
            f"近5日法人合計賣超{abs(inst_sum_5d_lots):.0f}張，非法人買超型態",
        )

    ratio = margin_chg_5d_lots / max(inst_sum_5d_lots, 1)
    if ratio <= 0:
        score = 100.0
    elif ratio >= 1:
        score = 0.0
    else:
        score = 100.0 * (1 - ratio)

    if margin_chg_5d_lots <= 0:
        fact = f"法人買超{inst_sum_5d_lots:.0f}張、融資減{abs(margin_chg_5d_lots):.0f}張"
    else:
        fact = f"法人買超{inst_sum_5d_lots:.0f}張、融資增{margin_chg_5d_lots:.0f}張"

    return score, fact


def score_mild_volume_uptrend(code, dates_20d, price_by_date):
    """維度3：近5日收盤走高，且量能倍數落在1.0~1.8 -> 高分；爆量扣分。"""
    idx_map = {d: i for i, d in enumerate(dates_20d)}
    if len(dates_20d) < RECENT_WINDOW + VOLUME_RATIO_LOOKBACK + 1:
        return NEUTRAL_SCORE, "近期交易日數不足，無法計算量能倍數"

    today = dates_20d[-1]
    d_minus_5 = dates_20d[-1 - RECENT_WINDOW]
    close_today = price_by_date[today][0]
    close_5d_ago = price_by_date[d_minus_5][0]
    uptrend = close_today > close_5d_ago

    vol_base_dates = dates_20d[-1 - VOLUME_RATIO_LOOKBACK : -1]
    avg_vol_5d = sum(price_by_date[d][1] for d in vol_base_dates) / len(vol_base_dates)
    vol_today = price_by_date[today][1]
    if avg_vol_5d <= 0:
        return NEUTRAL_SCORE, "近5日均量為0，無法計算量能倍數"
    ratio = vol_today / avg_vol_5d

    lo, hi = VOL_HIGH_BAND
    if ratio > VOL_EXTREME_HIGH:
        vol_score = VOL_SCORE_EXTREME_HIGH
    elif ratio > hi:
        # (hi, EXTREME_HIGH] 區間，從100線性降到 VOL_SCORE_ABOVE_BAND_FLOOR
        span = VOL_EXTREME_HIGH - hi
        vol_score = 100 - (100 - VOL_SCORE_ABOVE_BAND_FLOOR) * (ratio - hi) / span
    elif ratio >= lo:
        vol_score = 100
    elif ratio >= VOL_LOW_CUT:
        # [VOL_LOW_CUT, lo) 區間，從 VOL_SCORE_BELOW_BAND_MIN 線性升到 VOL_SCORE_BELOW_BAND_MAX
        span = lo - VOL_LOW_CUT
        vol_score = VOL_SCORE_BELOW_BAND_MIN + (
            VOL_SCORE_BELOW_BAND_MAX - VOL_SCORE_BELOW_BAND_MIN
        ) * (ratio - VOL_LOW_CUT) / span
    else:
        vol_score = VOL_SCORE_LOW

    score = vol_score if uptrend else min(vol_score, NO_UPTREND_SCORE_CAP)
    trend_word = "走高" if uptrend else "未走高"
    fact = f"量能{ratio:.1f}倍、近5日收盤{trend_word}"
    return score, fact


def score_relative_strength(code, dates_20d, price_by_date, taiex_by_date):
    if len(dates_20d) < LOOKBACK_DAYS:
        return NEUTRAL_SCORE, "近20日資料不足"
    d_first, d_last = dates_20d[0], dates_20d[-1]
    if d_first not in taiex_by_date or d_last not in taiex_by_date:
        return NEUTRAL_SCORE, "缺大盤指數資料"

    stock_ret = (price_by_date[d_last][0] / price_by_date[d_first][0] - 1) * 100
    taiex_ret = (taiex_by_date[d_last] / taiex_by_date[d_first] - 1) * 100
    rs = stock_ret - taiex_ret

    score = max(0.0, min(100.0, 50 + rs * RS_SCALE))
    sign = "強於" if rs >= 0 else "弱於"
    fact = f"{sign}大盤{abs(rs):.1f}個百分點（個股{stock_ret:.1f}% vs 大盤{taiex_ret:.1f}%）"
    return score, fact


# ============================================================
# 主流程
# ============================================================


def main():
    price_series, names = load_price_series()
    inst_series = load_institutional_series()
    margin_series = load_margin_series()
    taiex_rows = load_taiex_series()
    taiex_by_date = dict(taiex_rows)

    all_dates = sorted({d for rows in price_series.values() for d, *_ in rows})
    report_date = all_dates[-1]

    qualified = []
    for code, rows in price_series.items():
        passes, avg_value_20d, recent20 = passes_quality_gate(rows)
        if not passes:
            continue
        qualified.append((code, rows, avg_value_20d, recent20))

    ranked = []
    for code, rows, avg_value_20d, recent20 in qualified:
        dates_20d = [r[0] for r in recent20]
        price_by_date = {r[0]: (r[1], r[2], r[3]) for r in rows}

        s1, f1 = score_institutional_streak(code, dates_20d, inst_series)
        s2, f2 = score_buy_without_retail(code, dates_20d, inst_series, margin_series)
        s3, f3 = score_mild_volume_uptrend(code, dates_20d, price_by_date)
        s4, f4 = score_relative_strength(code, dates_20d, price_by_date, taiex_by_date)

        total = (
            s1 * WEIGHT_INSTITUTIONAL_STREAK
            + s2 * WEIGHT_BUY_WITHOUT_RETAIL
            + s3 * WEIGHT_MILD_VOLUME_UPTREND
            + s4 * WEIGHT_RELATIVE_STRENGTH
        )

        last_close = price_by_date[dates_20d[-1]][0]
        prev_dates = [d for d in rows if d[0] < dates_20d[-1]]
        if prev_dates:
            prev_close = prev_dates[-1][1]
            change_pct = (last_close / prev_close - 1) * 100
        else:
            change_pct = None

        ranked.append(
            {
                "code": code,
                "name": names.get(code, ""),
                "total_score": round(total, 2),
                "dimensions": {
                    "institutional_streak": {
                        "weight": WEIGHT_INSTITUTIONAL_STREAK,
                        "score": round(s1, 2),
                        "fact": f1,
                    },
                    "buy_without_retail_follow": {
                        "weight": WEIGHT_BUY_WITHOUT_RETAIL,
                        "score": round(s2, 2),
                        "fact": f2,
                    },
                    "mild_volume_uptrend": {
                        "weight": WEIGHT_MILD_VOLUME_UPTREND,
                        "score": round(s3, 2),
                        "fact": f3,
                    },
                    "relative_strength": {
                        "weight": WEIGHT_RELATIVE_STRENGTH,
                        "score": round(s4, 2),
                        "fact": f4,
                    },
                },
                "close": last_close,
                "change_pct": round(change_pct, 2) if change_pct is not None else None,
                "avg_value_20d": round(avg_value_20d, 0),
            }
        )

    ranked.sort(key=lambda r: r["total_score"], reverse=True)
    top = ranked[:TOP_N]

    output = {
        "report_date": report_date,
        "qualified_count": len(qualified),
        "ranked_count": len(top),
        "thresholds": {
            "lookback_days": LOOKBACK_DAYS,
            "min_avg_value_20d": MIN_AVG_VALUE_20D,
            "market_cap_threshold_ntd": MARKET_CAP_THRESHOLD,
            "market_cap_proxy_min_avg_value_20d": PROXY_MIN_AVG_VALUE_20D,
        },
        "weights": {
            "institutional_streak": WEIGHT_INSTITUTIONAL_STREAK,
            "buy_without_retail_follow": WEIGHT_BUY_WITHOUT_RETAIL,
            "mild_volume_uptrend": WEIGHT_MILD_VOLUME_UPTREND,
            "relative_strength": WEIGHT_RELATIVE_STRENGTH,
        },
        "notes": NOTES,
        "ranking": top,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
