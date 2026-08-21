#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
讀 output/leaderboard.json，用純模板（填空，不生成也不評論）產生 Threads 文案，
存到 output/caption.txt。

事實文字（法人連買/融資/量能）一律直接取自 JSON 裡既有的 dimensions[...].fact
欄位（必要時只做「取子字串」等級的擷取，例如從「法人買超X張、融資減Y張」裡
挑出「融資減Y張」那一段），不新增任何詮釋或形容詞。

法人資金流向段落：讀 data/institutional_flow.csv 裡跟 leaderboard.json 同一個
report_date 的那一列（全市場三大法人買賣金額統計，新台幣元），換算成「億元」
呈現。找不到對應日期（例如該腳本尚未執行過、或當天查無資料）就整段省略，
不影響其餘文案照常產出。

資金流向前幾大類股段落：讀 output/sector_flow.json（analyze_sector_flow.py
的輸出，同樣核對 report_date 是否對得上），列出法人淨買超金額前幾大產業
類別，各產業下再列出淨買超金額最高的幾檔個股。report_date 對不上或
sectors 是空陣列（例如當天法人資料還沒發布、或找不到產業分類對照）就整段
省略，不影響其餘文案照常產出。

全長（含標點）目標控制在 150~450 字（含新增的法人資金流向、資金流向類股
兩個段落，比只有排行榜時的區間更寬）：超過上限就把每檔的事實從 3 項縮為
2 項（保留法人連買、融資，捨量能）重新組一次。

硬性禁用詞檢查：完稿若命中禁用詞清單，直接報錯、不寫檔（防止未來誤改模板
或誤植文字，混入帶有多空立場的詞彙）。
"""

import csv
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
INPUT_JSON = REPO_ROOT / "output" / "leaderboard.json"
OUTPUT_TXT = REPO_ROOT / "output" / "caption.txt"
FLOW_CSV = REPO_ROOT / "data" / "institutional_flow.csv"
SECTOR_FLOW_JSON = REPO_ROOT / "output" / "sector_flow.json"

TOP_N_CAPTION = 3
LENGTH_MIN = 150
LENGTH_MAX = 450

# 硬性禁用詞：文案含任一詞就報錯停止，防止未來誤改模板混入帶多空立場的用語。
BANNED_WORDS = [
    "看好",
    "可布局",
    "上漲",
    "起漲",
    "續強",
    "值得關注",
    "逢低",
    "強勢",
]

FOOTER_TEXT = (
    "本榜依法人買超、融資變化、量價、相對強度四項 TWSE 公開數據整理,"
    "不代表買賣建議,投資請自行評估。"
)
HASHTAGS = "#台股 #法人籌碼"


def fact_streak(dims):
    """法人連買事實：dimensions.institutional_streak.fact 原字串，不擷取。"""
    return dims["institutional_streak"]["fact"]


def fact_margin(dims):
    """融資事實：從 buy_without_retail_follow.fact 裡擷取「融資減/增X張」那段。

    原始 fact 是「法人買超X張、融資減Y張」的組合句；這裡只取子字串，不新增文字。
    缺資料或法人未買超的情況，原字串本身就沒有這個子字串，直接照原字串輸出。

    注意：render_card.py 的 build_tags() 對同一個 fact 欄位也做了類似擷取（給
    圖卡 tag 用，格式不同：例如「融資減Y張」相同但法人連買/量能是不同呈現）。
    若之後 score_stocks.py 改了 fact 的組句方式，這兩處的 regex 都要一併檢查。
    """
    text = dims["buy_without_retail_follow"]["fact"]
    m = re.search(r"融資(?:減|增)\d+張", text)
    return m.group(0) if m else text


def fact_volume(dims):
    """量能事實：從 mild_volume_uptrend.fact 裡擷取開頭「量能X倍」那段。

    原始 fact 是「量能X倍、近5日收盤走高/未走高」；這裡只取「、」前半段子字串。
    """
    text = dims["mild_volume_uptrend"]["fact"]
    m = re.match(r"量能[\d.]+倍", text)
    return m.group(0) if m else text


def build_stock_line(rank, entry, n_facts):
    facts_all = [
        fact_streak(entry["dimensions"]),
        fact_margin(entry["dimensions"]),
        fact_volume(entry["dimensions"]),
    ]
    facts = facts_all[:n_facts]
    return f"{rank}. {entry['name']}({entry['code']}):{'、'.join(facts)}"


def load_flow_row(report_date):
    """讀 data/institutional_flow.csv 裡 Date == report_date 的那一列；
    檔案不存在或找不到對應日期就回傳 None（呼叫端整段省略，不報錯）。
    """
    if not FLOW_CSV.exists():
        return None
    with FLOW_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["Date"] == report_date:
                return row
    return None


def format_100m(raw):
    """新台幣元轉「億元」，回傳 (買超/賣超/持平, 金額絕對值) 或 None（缺資料）。"""
    if raw in (None, ""):
        return None
    net = float(raw)
    if net > 0:
        sign = "買超"
    elif net < 0:
        sign = "賣超"
    else:
        sign = "持平"
    return sign, abs(net) / 1e8


def build_flow_section(flow_row):
    """三大法人全市場買賣金額段落；缺資料回傳 None（呼叫端整段省略）。"""
    if flow_row is None:
        return None

    parts = []
    for label, key in [("外資", "ForeignNet"), ("投信", "TrustNet"), ("自營商", "DealerNet")]:
        r = format_100m(flow_row.get(key))
        if r is None:
            continue
        sign, amt = r
        parts.append(f"{label}{sign}{amt:.1f}億元")
    if not parts:
        return None

    total_str = ""
    total = format_100m(flow_row.get("TotalNet"))
    if total is not None:
        sign, amt = total
        total_str = f"，三大法人合計{sign}{amt:.1f}億元"

    return f"【法人資金流向】{'、'.join(parts)}{total_str}。"


def load_sector_flow(report_date):
    """讀 output/sector_flow.json，核對 report_date 是否對得上；對不上、
    檔案不存在、或 sectors 是空陣列都回傳 None（呼叫端整段省略，不報錯）。
    """
    if not SECTOR_FLOW_JSON.exists():
        return None
    data = json.loads(SECTOR_FLOW_JSON.read_text(encoding="utf-8"))
    if data.get("report_date") != report_date:
        return None
    return data.get("sectors") or None


def build_sector_flow_section(sectors):
    """資金流向前幾大類股段落；缺資料回傳 None（呼叫端整段省略）。"""
    if not sectors:
        return None

    lines = [f"【資金流向前{len(sectors)}大類股】"]
    for i, sec in enumerate(sectors, 1):
        stocks = sec.get("stocks") or []
        if not stocks:
            continue
        stock_names = "、".join(f"{s['name']}({s['code']})" for s in stocks)
        amt = sec["net_buy_value_twd"] / 1e8
        lines.append(f"{i}. {sec['sector']}（合計淨買超{amt:.1f}億元）：{stock_names}")

    if len(lines) == 1:  # 只有標題、沒有任何產業列出來
        return None
    return "\n".join(lines)


def build_caption(data, n_facts, flow_row, sectors):
    header = f"【法人吸籌榜 {data['report_date']}】"
    intro = f"今日通過優質門檻 {data['qualified_count']} 檔,依四項公開數據排序前三:"
    top = data["ranking"][:TOP_N_CAPTION]
    lines = [build_stock_line(i + 1, e, n_facts) for i, e in enumerate(top)]

    parts = [header, intro, *lines]
    flow_section = build_flow_section(flow_row)
    if flow_section:
        parts += ["", flow_section]
    sector_section = build_sector_flow_section(sectors)
    if sector_section:
        parts += ["", sector_section]
    parts += ["", FOOTER_TEXT, HASHTAGS]
    return "\n".join(parts)


def check_banned_words(caption):
    hits = [w for w in BANNED_WORDS if w in caption]
    return hits


def main():
    data = json.loads(INPUT_JSON.read_text(encoding="utf-8"))

    if len(data["ranking"]) < TOP_N_CAPTION:
        print(
            f"[警告] 進榜檔數只有 {len(data['ranking'])} 檔，不足前三，"
            "文案會照實際檔數列出。",
            file=sys.stderr,
        )

    flow_row = load_flow_row(data["report_date"])
    if flow_row is None:
        print(
            f"[警告] data/institutional_flow.csv 找不到 {data['report_date']} 這天的資料，"
            "文案將省略法人資金流向段落。",
            file=sys.stderr,
        )

    sectors = load_sector_flow(data["report_date"])
    if not sectors:
        print(
            f"[警告] output/sector_flow.json 找不到 {data['report_date']} 這天的產業資料，"
            "文案將省略資金流向類股段落。",
            file=sys.stderr,
        )

    caption = build_caption(data, n_facts=3, flow_row=flow_row, sectors=sectors)
    if len(caption) > LENGTH_MAX:
        print(
            f"[調整] 3項事實版本 {len(caption)} 字超過 {LENGTH_MAX} 字上限，"
            "改用每檔2項事實（法人連買、融資）重組。",
            file=sys.stderr,
        )
        caption = build_caption(data, n_facts=2, flow_row=flow_row, sectors=sectors)

    length = len(caption)
    print(f"文案長度：{length} 字（目標 {LENGTH_MIN}~{LENGTH_MAX} 字）", file=sys.stderr)
    if not (LENGTH_MIN <= length <= LENGTH_MAX):
        print(
            f"[注意] 目前長度 {length} 字不在 {LENGTH_MIN}~{LENGTH_MAX} 字區間內"
            "（超過200字已嘗試縮為2項事實；不足150字則照實際資料輸出，不硬湊字數）。",
            file=sys.stderr,
        )

    hits = check_banned_words(caption)
    if hits:
        print(f"[錯誤] 文案命中禁用詞：{hits}，停止產出，不寫入檔案。", file=sys.stderr)
        sys.exit(1)

    OUTPUT_TXT.write_text(caption, encoding="utf-8")
    print(f"已寫入：{OUTPUT_TXT}", file=sys.stderr)
    print(caption)


if __name__ == "__main__":
    main()
