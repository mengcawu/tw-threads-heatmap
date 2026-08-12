#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
讀 output/leaderboard.json，用純模板（填空，不生成也不評論）產生 Threads 文案，
存到 output/caption.txt。

事實文字（法人連買/融資/量能）一律直接取自 JSON 裡既有的 dimensions[...].fact
欄位（必要時只做「取子字串」等級的擷取，例如從「法人買超X張、融資減Y張」裡
挑出「融資減Y張」那一段），不新增任何詮釋或形容詞。

全長（含標點）控制在 150~200 字：超過 200 字就把每檔的事實從 3 項縮為 2 項
（保留法人連買、融資，捨量能）重新組一次。

硬性禁用詞檢查：完稿若命中禁用詞清單，直接報錯、不寫檔（防止未來誤改模板
或誤植文字，混入帶有多空立場的詞彙）。
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
INPUT_JSON = REPO_ROOT / "output" / "leaderboard.json"
OUTPUT_TXT = REPO_ROOT / "output" / "caption.txt"

TOP_N_CAPTION = 3
LENGTH_MIN = 150
LENGTH_MAX = 200

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


def build_caption(data, n_facts):
    header = f"【法人吸籌榜 {data['report_date']}】"
    intro = f"今日通過優質門檻 {data['qualified_count']} 檔,依四項公開數據排序前三:"
    top = data["ranking"][:TOP_N_CAPTION]
    lines = [build_stock_line(i + 1, e, n_facts) for i, e in enumerate(top)]
    return "\n".join([header, intro, *lines, "", FOOTER_TEXT, HASHTAGS])


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

    caption = build_caption(data, n_facts=3)
    if len(caption) > LENGTH_MAX:
        print(
            f"[調整] 3項事實版本 {len(caption)} 字超過 {LENGTH_MAX} 字上限，"
            "改用每檔2項事實（法人連買、融資）重組。",
            file=sys.stderr,
        )
        caption = build_caption(data, n_facts=2)

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
