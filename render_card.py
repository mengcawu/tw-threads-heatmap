#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 score_stocks.py 的吸籌榜 JSON 輸出，畫成一張 1080x1080 的社群圖卡 PNG。

流程：
    1. 執行 score_stocks.py，讀取其 stdout 的完整 JSON（不重新計算分數，
       單純視覺化既有結果），同時另存一份到 output/leaderboard.json 供之後發布用。
    2. 榜單（score_stocks.py 已經是 veto 後、最多 TOP_N=10 檔的最終名單）全部畫出，
       依「實際檔數」動態算列高／字級縮放比例，8檔、10檔、12檔都不裁切、不留大空白。
       採兩階段渲染：先用預設縮放畫一次、實際量測 .list 可用高度（不用猜的常數），
       算出精確縮放比例後再重畫一次，避免估計值誤差造成裁切或空白。
    3. 用 Playwright + Chromium 以 2x 解析度截圖，再縮回 1080x1080，
       文字邊緣更乾淨（縮圖可讀性優先）。

輸出：output/leaderboard_card.png、output/leaderboard.json
"""

import html
import json
import re
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = REPO_ROOT / "output"
OUTPUT_PNG = OUTPUT_DIR / "leaderboard_card.png"
OUTPUT_JSON = OUTPUT_DIR / "leaderboard.json"
HTML_SCRATCH = Path("/tmp/leaderboard_card_render.html")

CARD_MAX_ROWS = 15  # 防禦性上限；score_stocks.py 目前 veto 後最多輸出 10 檔
CANVAS = 1080
SCALE = 2  # 先用 2x 截圖再縮小，字更清晰

CARD_PAD_TOP = 40
CARD_PAD_BOTTOM = 28
# 安全係數：即使量到精確可用高度，仍只填到這個比例，留一點餘裕防止字型
# 渲染的些微差異（sub-pixel/行高）造成裁切。
SAFETY_FACTOR = 0.97

# 版面基準規格（在 N=10 檔、scale=1.0 時，是實測不裁切/不留白的版本）。
# 檔數變動時，整組數值依 compute_scale() 算出的縮放比例一起放大/縮小，
# 而不是只調列高：字級、內距、tag 間距全部同步縮放，維持比例協調。
BASE_SPEC = {
    "row_top3_h": 98,
    "row_rest_h": 58,
    "rank_top3_font": 44,
    "rank_top3_w": 66,
    "rank_rest_font": 26,
    "rank_w": 54,
    "name_top3_font": 40,
    "name_rest_font": 27,
    "code_top3_font": 24,
    "code_rest_font": 18,
    "close_top3_font": 32,
    "close_top3_w": 130,
    "close_rest_font": 24,
    "close_rest_w": 110,
    "chg_top3_font": 30,
    "chg_top3_w": 130,
    "chg_top3_pad": 6,
    "chg_rest_font": 21,
    "chg_rest_w": 108,
    "chg_rest_pad": 3,
    "score_top3_font": 34,
    "score_top3_w": 96,
    "score_top3_pad": 6,
    "score_rest_font": 22,
    "score_rest_w": 78,
    "score_rest_pad": 3,
    "tag_top3_font": 20,
    "tag_rest_font": 14,
    "tag_pad_v": 3,
    "tag_pad_h": 10,
    "tag_rest_pad_v": 1,
    "tag_rest_pad_h": 7,
    "tags_top3_margin": 6,
    "tags_rest_margin": 2,
    "tags_top3_padleft": 70,
    "tags_rest_padleft": 62,
    "row_gap": 16,
    "tags_gap": 8,
}
# 縮放比例上下限：避免檔數極少時字放到誇張大，或極多時縮到不可讀。
# 卡片寬度固定 1080px，不像高度能靠檔數自然分配——SCALE_MAX 刻意保守（而非跟
# SCALE_MIN 對稱地衝到 1.6），確保就算檔數很少、垂直方向有很多空間可以放大，
# 兩側欄位（排名/收盤/漲跌/分數色塊）+ 股名代號 加總寬度仍不會超過 1080，
# 不然股名會被擠到觸發 ellipsis 截斷，或欄位彼此重疊。這個上限是用最壞情況
# （3個中文字股名 + 4碼代號）反推、留了緩衝的結果，見開發過程驗證。
SCALE_MIN = 0.75
SCALE_MAX = 1.35

CHROMIUM_PATH = "/opt/pw-browsers/chromium"

# ---- 深色主題色票（取自 dataviz skill 的驗證色票，dark 模式） ----
COLOR_SURFACE = "#1a1a19"
COLOR_TEXT_PRIMARY = "#ffffff"
COLOR_TEXT_SECONDARY = "#c3c2b7"
COLOR_TEXT_MUTED = "#898781"
COLOR_HAIRLINE = "#2c2c2a"
COLOR_BASELINE = "#383835"
COLOR_UP_RED = "#d03b3b"  # 台股慣例：漲＝紅（status/critical）
COLOR_DOWN_GREEN = "#0ca30c"  # 台股慣例：跌＝綠（status/good）
SCORE_LOW = (0x18, 0x4F, 0x95)  # 分數低：sequential blue 深色端
SCORE_HIGH = (0x6D, 0xA7, 0xEC)  # 分數高：sequential blue 亮色端


def run_scoring():
    result = subprocess.run(
        [sys.executable, "score_stocks.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def compute_scale(n_rows, list_budget):
    """依實際檔數與量到的可用高度，算出 BASE_SPEC 該放大/縮小多少倍。"""
    if n_rows <= 0:
        return 1.0
    top3 = min(3, n_rows)
    rest = max(0, n_rows - 3)
    ref_total = top3 * BASE_SPEC["row_top3_h"] + rest * BASE_SPEC["row_rest_h"]
    if ref_total <= 0:
        return 1.0
    scale = (list_budget * SAFETY_FACTOR) / ref_total
    return max(SCALE_MIN, min(SCALE_MAX, scale))


def scaled_spec(scale):
    return {k: v * scale for k, v in BASE_SPEC.items()}


def lerp(a, b, t):
    return a + (b - a) * t


def score_color(score, score_min, score_max):
    # 依本次上榜檔數的實際分數區間正規化，而非固定 0~100，
    # 讓深淺差異在分數集中的情況下仍清楚可辨。
    span = score_max - score_min
    t = 0.5 if span <= 0 else (score - score_min) / span
    t = max(0.0, min(1.0, t))
    r = round(lerp(SCORE_LOW[0], SCORE_HIGH[0], t))
    g = round(lerp(SCORE_LOW[1], SCORE_HIGH[1], t))
    b = round(lerp(SCORE_LOW[2], SCORE_HIGH[2], t))
    # 相對亮度（sRGB 近似），決定分數色塊上要用白字還是深字
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    text_color = "#0b0b0b" if luminance > 0.6 else "#ffffff"
    return f"#{r:02x}{g:02x}{b:02x}", text_color


def build_tags(dims):
    """從四維分數/事實字串擷取 2~3 個精簡標籤（法人連續買超／融資／量能）。"""
    tags = []

    streak_fact = dims["institutional_streak"]["fact"]
    m = re.search(r"(外資|投信)連(\d+)日", streak_fact)
    tags.append(f"{m.group(1)}連{m.group(2)}日" if m else "無連續買超")

    retail_fact = dims["buy_without_retail_follow"]["fact"]
    m = re.search(r"融資(減|增)(\d+)張", retail_fact)
    if m:
        tags.append(f"融資{m.group(1)}{m.group(2)}張")
    else:
        tags.append("法人未買超" if "賣超" in retail_fact else "融資資料不足")

    vol_fact = dims["mild_volume_uptrend"]["fact"]
    m = re.search(r"量能([\d.]+)倍、近5日收盤(走高|未走高)", vol_fact)
    if m:
        ratio, trend = float(m.group(1)), m.group(2)
        if trend == "走高" and 1.0 <= ratio <= 1.8:
            tags.append(f"量溫和({ratio:.1f}倍)")
        elif ratio > 2.5:
            tags.append(f"爆量({ratio:.1f}倍)")
        elif ratio < 0.8:
            tags.append(f"量縮({ratio:.1f}倍)")
        else:
            tags.append(f"量能{ratio:.1f}倍")
    else:
        tags.append("量能資料不足")

    return tags


def fmt_close(v):
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v is not None else "—"


def fmt_pct(v):
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}%"


def build_row_html(rank, entry, score_min, score_max):
    is_top3 = rank <= 3
    tier_class = "row-top3" if is_top3 else "row-rest"

    chg = entry["change_pct"]
    chg_class = "chg-up" if (chg or 0) > 0 else ("chg-down" if (chg or 0) < 0 else "chg-flat")
    score_bg, score_text_color = score_color(entry["total_score"], score_min, score_max)
    tags = build_tags(entry["dimensions"])

    name = html.escape(entry["name"])
    code = html.escape(entry["code"])

    tag_html = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in tags)

    return f"""
    <div class="row {tier_class}">
      <div class="row-main">
        <div class="rank">{rank}</div>
        <div class="name-block">
          <span class="name">{name}</span><span class="code">{code}</span>
        </div>
        <div class="close">{fmt_close(entry['close'])}</div>
        <div class="chg {chg_class}">{fmt_pct(chg)}</div>
        <div class="score-pill" style="background:{score_bg}; color:{score_text_color};">
          {entry['total_score']:.0f}
        </div>
      </div>
      <div class="tags">{tag_html}</div>
    </div>
    """


def build_html(data, scale):
    rows = data["ranking"][:CARD_MAX_ROWS]
    n = len(rows)
    sv = scaled_spec(scale)

    scores = [e["total_score"] for e in rows] or [0]
    score_min, score_max = min(scores), max(scores)
    rows_html = "".join(
        build_row_html(i + 1, e, score_min, score_max) for i, e in enumerate(rows)
    )

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  html, body {{
    width: {CANVAS}px;
    height: {CANVAS}px;
    background: {COLOR_SURFACE};
    font-family: "Noto Sans CJK TC", "Noto Sans TC", sans-serif;
    color: {COLOR_TEXT_PRIMARY};
    overflow: hidden;
  }}
  .card {{
    width: {CANVAS}px;
    height: {CANVAS}px;
    display: flex;
    flex-direction: column;
    padding: {CARD_PAD_TOP}px 44px {CARD_PAD_BOTTOM}px;
  }}
  .header {{
    flex: 0 0 auto;
    padding-bottom: 18px;
    border-bottom: 1px solid {COLOR_HAIRLINE};
    margin-bottom: 14px;
  }}
  .header-top {{
    display: flex;
    align-items: baseline;
    justify-content: space-between;
  }}
  .title {{
    font-size: 50px;
    font-weight: 700;
    letter-spacing: 1px;
  }}
  .date {{
    font-size: 28px;
    font-weight: 500;
    color: {COLOR_TEXT_SECONDARY};
    font-variant-numeric: tabular-nums;
  }}
  .subtitle {{
    margin-top: 8px;
    font-size: 24px;
    color: {COLOR_TEXT_MUTED};
  }}
  .subtitle b {{
    color: {COLOR_TEXT_SECONDARY};
    font-weight: 700;
  }}
  .list {{
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    justify-content: center;
    min-height: 0;
    overflow: hidden;
  }}
  .row {{
    display: flex;
    flex-direction: column;
    justify-content: center;
    border-bottom: 1px solid {COLOR_HAIRLINE};
    overflow: hidden;
  }}
  .row-top3 {{ height: {sv['row_top3_h']:.1f}px; flex: none; }}
  .row-rest {{ height: {sv['row_rest_h']:.1f}px; flex: none; }}
  .row-main {{
    display: flex;
    align-items: center;
    gap: {sv['row_gap']:.1f}px;
  }}
  .rank {{
    flex: 0 0 auto;
    width: {sv['rank_w']:.1f}px;
    text-align: center;
    font-weight: 700;
    color: {COLOR_TEXT_MUTED};
    font-variant-numeric: tabular-nums;
  }}
  .row-top3 .rank {{
    width: {sv['rank_top3_w']:.1f}px;
    font-size: {sv['rank_top3_font']:.1f}px;
    color: {COLOR_TEXT_PRIMARY};
  }}
  .row-rest .rank {{ font-size: {sv['rank_rest_font']:.1f}px; }}
  .name-block {{
    flex: 1 1 auto;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .row-top3 .name {{ font-size: {sv['name_top3_font']:.1f}px; font-weight: 700; }}
  .row-rest .name {{ font-size: {sv['name_rest_font']:.1f}px; font-weight: 700; }}
  .code {{
    color: {COLOR_TEXT_MUTED};
    font-variant-numeric: tabular-nums;
    margin-left: 8px;
  }}
  .row-top3 .code {{ font-size: {sv['code_top3_font']:.1f}px; }}
  .row-rest .code {{ font-size: {sv['code_rest_font']:.1f}px; }}
  .close {{
    flex: 0 0 auto;
    text-align: right;
    font-variant-numeric: tabular-nums;
    color: {COLOR_TEXT_SECONDARY};
  }}
  .row-top3 .close {{ width: {sv['close_top3_w']:.1f}px; font-size: {sv['close_top3_font']:.1f}px; }}
  .row-rest .close {{ width: {sv['close_rest_w']:.1f}px; font-size: {sv['close_rest_font']:.1f}px; }}
  .chg {{
    flex: 0 0 auto;
    text-align: center;
    border-radius: 8px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }}
  .row-top3 .chg {{
    width: {sv['chg_top3_w']:.1f}px; font-size: {sv['chg_top3_font']:.1f}px;
    padding: {sv['chg_top3_pad']:.1f}px 0;
  }}
  .row-rest .chg {{
    width: {sv['chg_rest_w']:.1f}px; font-size: {sv['chg_rest_font']:.1f}px;
    padding: {sv['chg_rest_pad']:.1f}px 0;
  }}
  .chg-up {{ background: rgba(208,59,59,0.22); color: #ff8b8b; }}
  .chg-down {{ background: rgba(12,163,12,0.22); color: #59e05f; }}
  .chg-flat {{ background: rgba(137,135,129,0.2); color: {COLOR_TEXT_SECONDARY}; }}
  .score-pill {{
    flex: 0 0 auto;
    text-align: center;
    border-radius: 10px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }}
  .row-top3 .score-pill {{
    width: {sv['score_top3_w']:.1f}px; font-size: {sv['score_top3_font']:.1f}px;
    padding: {sv['score_top3_pad']:.1f}px 0;
  }}
  .row-rest .score-pill {{
    width: {sv['score_rest_w']:.1f}px; font-size: {sv['score_rest_font']:.1f}px;
    padding: {sv['score_rest_pad']:.1f}px 0;
  }}
  .tags {{
    display: flex;
    gap: {sv['tags_gap']:.1f}px;
    margin-top: {sv['tags_top3_margin']:.1f}px;
    padding-left: {sv['tags_top3_padleft']:.1f}px;
  }}
  .row-rest .tags {{
    padding-left: {sv['tags_rest_padleft']:.1f}px;
    margin-top: {sv['tags_rest_margin']:.1f}px;
  }}
  .tag {{
    background: rgba(255,255,255,0.08);
    color: {COLOR_TEXT_SECONDARY};
    border-radius: 6px;
    padding: {sv['tag_pad_v']:.1f}px {sv['tag_pad_h']:.1f}px;
    white-space: nowrap;
  }}
  .row-top3 .tag {{ font-size: {sv['tag_top3_font']:.1f}px; }}
  .row-rest .tag {{
    font-size: {sv['tag_rest_font']:.1f}px;
    padding: {sv['tag_rest_pad_v']:.1f}px {sv['tag_rest_pad_h']:.1f}px;
  }}
  .footer {{
    flex: 0 0 auto;
    margin-top: 14px;
    padding-top: 14px;
    border-top: 1px solid {COLOR_HAIRLINE};
    text-align: center;
    font-size: 17px;
    color: {COLOR_TEXT_MUTED};
  }}
</style>
</head>
<body>
  <div class="card">
    <div class="header">
      <div class="header-top">
        <div class="title">法人吸籌榜</div>
        <div class="date">{html.escape(data['report_date'])}</div>
      </div>
      <div class="subtitle">
        通過優質門檻 <b>{data['qualified_count']}</b> 檔　進榜 <b>{n}</b> 檔
      </div>
    </div>
    <div class="list">
      {rows_html}
    </div>
    <div class="footer">
      資料來源：TWSE公開資訊，依法人買超／融資／量價／相對強度整理，非投資建議
    </div>
  </div>
</body>
</html>
"""


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    data = run_scoring()

    OUTPUT_JSON.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已另存：{OUTPUT_JSON}", file=sys.stderr)

    n_rows = len(data["ranking"][:CARD_MAX_ROWS])

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        page = browser.new_page(
            viewport={"width": CANVAS, "height": CANVAS},
            device_scale_factor=SCALE,
        )

        # 第一階段：用 scale=1.0 畫一次，只為了量出 .list 實際可用高度
        # （header/footer 的真實渲染高度不用猜，直接量）。
        probe_html = build_html(data, scale=1.0)
        HTML_SCRATCH.write_text(probe_html, encoding="utf-8")
        page.goto(f"file://{HTML_SCRATCH}")
        page.wait_for_timeout(100)
        list_budget = page.eval_on_selector(".list", "el => el.getBoundingClientRect().height")

        scale = compute_scale(n_rows, list_budget)

        # 第二階段：用量出來的精確縮放比例正式畫一次
        final_html = build_html(data, scale=scale)
        HTML_SCRATCH.write_text(final_html, encoding="utf-8")
        page.goto(f"file://{HTML_SCRATCH}")
        page.wait_for_timeout(100)
        page.screenshot(path=str(OUTPUT_PNG))
        browser.close()

    # 用 PIL 把 2x 截圖縮回 1080x1080，文字邊緣更乾淨
    from PIL import Image

    img = Image.open(OUTPUT_PNG)
    if img.size != (CANVAS, CANVAS):
        img = img.resize((CANVAS, CANVAS), Image.LANCZOS)
        img.save(OUTPUT_PNG)

    print(
        f"完成：{OUTPUT_PNG}（{img.size[0]}x{img.size[1]}，"
        f"畫出{n_rows}檔，量到可用高度{list_budget:.0f}px，縮放比例{scale:.2f}）",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
