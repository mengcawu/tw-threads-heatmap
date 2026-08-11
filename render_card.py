#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 score_stocks.py 的吸籌榜 JSON 輸出，畫成一張 1080x1080 的社群圖卡 PNG。

流程：
    1. 執行 score_stocks.py，讀取其 stdout 的完整 JSON（不重新計算分數，
       單純視覺化既有結果）。
    2. 取榜單前 12 檔，組成 HTML（深色主題，沿用 dataviz skill 的驗證色票）。
    3. 用 Playwright + Chromium 以 2x 解析度截圖，再縮回 1080x1080，
       文字邊緣更乾淨（縮圖可讀性優先）。

輸出：output/leaderboard_card.png
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
HTML_SCRATCH = Path("/tmp/leaderboard_card_render.html")

TOP_N_CARD = 12
CANVAS = 1080
SCALE = 2  # 先用 2x 截圖再縮小，字更清晰

# 版面高度預算（固定像素，避免 flex 內容撐爆導致截斷/重疊）：
# 40(頂padding) + 128(header) + 3*ROW_TOP3 + 9*ROW_REST + 62(footer) + 28(底padding) = 1080
CARD_PAD_TOP = 40
CARD_PAD_BOTTOM = 28
HEADER_HEIGHT = 128
FOOTER_HEIGHT = 62
ROW_TOP3_HEIGHT = 98
ROW_REST_HEIGHT = 58
_LIST_BUDGET = CANVAS - CARD_PAD_TOP - CARD_PAD_BOTTOM - HEADER_HEIGHT - FOOTER_HEIGHT
_LIST_USED = 3 * ROW_TOP3_HEIGHT + 9 * ROW_REST_HEIGHT
assert _LIST_USED <= _LIST_BUDGET, (
    f"row heights ({_LIST_USED}px) exceed available list budget ({_LIST_BUDGET}px)"
)

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


def lerp(a, b, t):
    return a + (b - a) * t


def score_color(score, score_min, score_max):
    # 依本次上榜前12檔的實際分數區間正規化，而非固定 0~100，
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


def build_html(data):
    top12 = data["ranking"][:TOP_N_CARD]
    scores = [e["total_score"] for e in top12]
    score_min, score_max = min(scores), max(scores)
    rows_html = "".join(
        build_row_html(i + 1, e, score_min, score_max) for i, e in enumerate(top12)
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
    padding: 40px 44px 28px;
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
  .row-top3 {{ height: {ROW_TOP3_HEIGHT}px; flex: none; }}
  .row-rest {{ height: {ROW_REST_HEIGHT}px; flex: none; }}
  .row-main {{
    display: flex;
    align-items: center;
    gap: 16px;
  }}
  .rank {{
    flex: 0 0 auto;
    width: 54px;
    text-align: center;
    font-weight: 700;
    color: {COLOR_TEXT_MUTED};
    font-variant-numeric: tabular-nums;
  }}
  .row-top3 .rank {{
    width: 66px;
    font-size: 44px;
    color: {COLOR_TEXT_PRIMARY};
  }}
  .row-rest .rank {{ font-size: 26px; }}
  .name-block {{
    flex: 1 1 auto;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .row-top3 .name {{ font-size: 40px; font-weight: 700; }}
  .row-rest .name {{ font-size: 27px; font-weight: 700; }}
  .code {{
    color: {COLOR_TEXT_MUTED};
    font-variant-numeric: tabular-nums;
    margin-left: 8px;
  }}
  .row-top3 .code {{ font-size: 24px; }}
  .row-rest .code {{ font-size: 18px; }}
  .close {{
    flex: 0 0 auto;
    text-align: right;
    font-variant-numeric: tabular-nums;
    color: {COLOR_TEXT_SECONDARY};
  }}
  .row-top3 .close {{ width: 130px; font-size: 32px; }}
  .row-rest .close {{ width: 110px; font-size: 24px; }}
  .chg {{
    flex: 0 0 auto;
    text-align: center;
    border-radius: 8px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }}
  .row-top3 .chg {{ width: 130px; font-size: 30px; padding: 6px 0; }}
  .row-rest .chg {{ width: 108px; font-size: 21px; padding: 3px 0; }}
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
  .row-top3 .score-pill {{ width: 96px; font-size: 34px; padding: 6px 0; }}
  .row-rest .score-pill {{ width: 78px; font-size: 22px; padding: 3px 0; }}
  .tags {{
    display: flex;
    gap: 8px;
    margin-top: 6px;
    padding-left: 70px;
  }}
  .row-rest .tags {{ padding-left: 62px; margin-top: 2px; }}
  .tag {{
    background: rgba(255,255,255,0.08);
    color: {COLOR_TEXT_SECONDARY};
    border-radius: 6px;
    padding: 3px 10px;
    white-space: nowrap;
  }}
  .row-top3 .tag {{ font-size: 20px; }}
  .row-rest .tag {{ font-size: 14px; padding: 1px 7px; }}
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
        通過優質門檻 <b>{data['qualified_count']}</b> 檔　進榜 <b>{data['ranked_count']}</b> 檔
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
    html_content = build_html(data)
    HTML_SCRATCH.write_text(html_content, encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM_PATH)
        page = browser.new_page(
            viewport={"width": CANVAS, "height": CANVAS},
            device_scale_factor=SCALE,
        )
        page.goto(f"file://{HTML_SCRATCH}")
        page.wait_for_timeout(150)  # 讓字型完成 layout
        page.screenshot(path=str(OUTPUT_PNG))
        browser.close()

    # 用 PIL 把 2x 截圖縮回 1080x1080，文字邊緣更乾淨
    from PIL import Image

    img = Image.open(OUTPUT_PNG)
    if img.size != (CANVAS, CANVAS):
        img = img.resize((CANVAS, CANVAS), Image.LANCZOS)
        img.save(OUTPUT_PNG)

    print(f"完成：{OUTPUT_PNG}（{img.size[0]}x{img.size[1]}）", file=sys.stderr)


if __name__ == "__main__":
    main()
