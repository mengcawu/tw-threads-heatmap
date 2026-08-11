#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 score_stocks.py 的吸籌榜 JSON 輸出，畫成一張 1080x1080 的社群圖卡 PNG。

視覺風格：深色金融科技數據卡片——純黑底、單一 emerald/teal 綠色主題、
卡片式層次、精緻留白。漲跌欄位維持台股慣例（紅漲綠跌），獨立於主題綠色之外。
資料與評分邏輯完全在 score_stocks.py，這支檔案只負責畫圖，不重算任何分數。

流程：
    1. 執行 score_stocks.py，讀取其 stdout 的完整 JSON（不重新計算分數，
       單純視覺化既有結果），同時另存一份到 output/leaderboard.json 供之後發布用。
    2. 榜單（score_stocks.py 已經是 veto 後、最多 TOP_N=10 檔的最終名單）全部畫出，
       依「實際檔數」動態算列高／字級縮放比例，8檔、10檔、12檔都不裁切、不留大空白。
       採兩階段渲染：先用預設縮放畫一次、實際量測榜單卡片內可用高度（不用猜的
       常數），算出精確縮放比例後再重畫一次，避免估計值誤差造成裁切或空白。
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

CARD_MAX_ROWS = 12  # 防禦性上限；score_stocks.py 目前 veto 後最多輸出 10 檔
CANVAS = 1080
SCALE = 2  # 先用 2x 截圖再縮小，字更清晰

# 安全係數：即使量到精確可用高度，仍只填到這個比例，留一點餘裕防止字型
# 渲染的些微差異（sub-pixel/行高）造成裁切。
SAFETY_FACTOR = 0.97

CHROMIUM_PATH = "/opt/pw-browsers/chromium"

# ============================================================
# 色票——深色金融科技風：純黑底 + 單一 emerald/teal 主題色
# ============================================================
COLOR_BG = "#0a0a0a"  # 頁面底色，接近純黑
COLOR_CARD_BG = "#161616"  # 榜單卡片底色，比頁面底色略淺，形成層次
COLOR_CARD_BORDER = "rgba(255,255,255,0.06)"
COLOR_TEXT_PRIMARY = "#f5f5f4"
COLOR_TEXT_SECONDARY = "#a3a29c"
COLOR_TEXT_MUTED = "#6b6a65"
COLOR_HAIRLINE = "rgba(255,255,255,0.06)"

# 主題綠（emerald/teal）——頂部膠囊標籤、分隔線、吸籌分色塊、chip 邊框都用這組。
COLOR_ACCENT = "#14b8a6"  # teal-500，主題色
COLOR_ACCENT_BRIGHT = "#5eead4"  # teal-300，分數高分端/強調用
COLOR_ACCENT_DIM = "#0f3d38"  # 深綠，分數低分端
COLOR_ACCENT_TINT_BG = "rgba(20,184,166,0.10)"  # chip 淡底
COLOR_ACCENT_TINT_BORDER = "rgba(20,184,166,0.35)"  # chip 細邊

# 例外：漲跌欄位維持台股慣例，紅漲綠跌，獨立於主題綠色，刻意選跟主題綠（teal，
# 偏藍的綠）色相不同的「跌」綠（偏黃的正綠），兩者不會被誤認成同一件事。
COLOR_UP_RED = "#f0483e"
COLOR_UP_RED_TINT = "rgba(240,72,62,0.14)"
COLOR_DOWN_GREEN = "#22c55e"
COLOR_DOWN_GREEN_TINT = "rgba(34,197,94,0.14)"

# 名次徽章：金／銀／銅，僅裝飾用途，不參與資料編碼。
COLOR_MEDAL_GOLD = "#f0c419"
COLOR_MEDAL_SILVER = "#c9ccd1"
COLOR_MEDAL_BRONZE = "#cd8a4e"

# ============================================================
# 版面基準規格（在 N=10 檔、scale=1.0 時，是實測不裁切/不留白的版本）。
# 檔數變動時，整組數值依 compute_scale() 算出的縮放比例一起放大/縮小。
# 「收盤/漲跌/吸籌分」三欄寬度所有列（含前三名）共用同一組，維持水平對齊；
# 只有名次徽章與股名字級依前3名/其餘分兩級，不影響右側三欄對齊。
# ============================================================
BASE_SPEC = {
    "row_top3_h": 96,
    "row_rest_h": 58,
    "dot_size": 8,
    "dot_gap": 14,
    "badge_col_w": 52,
    "badge_top3_size": 44,
    "badge_top3_font": 22,
    "badge_rest_font": 22,
    "name_gap": 16,
    "name_top3_font": 34,
    "name_rest_font": 24,
    "code_top3_font": 19,
    "code_rest_font": 15,
    "col_gap": 22,
    "close_col_w": 118,
    "close_top3_font": 27,
    "close_rest_font": 21,
    "chg_col_w": 98,
    "chg_top3_font": 21,
    "chg_top3_pad": 5,
    "chg_rest_font": 16,
    "chg_rest_pad": 3,
    "score_col_w": 76,
    "score_top3_font": 23,
    "score_top3_pad": 5,
    "score_rest_font": 18,
    "score_rest_pad": 3,
    "tag_top3_font": 17,
    "tag_rest_font": 13,
    "tag_pad_v": 4,
    "tag_pad_h": 10,
    "tag_rest_pad_v": 2,
    "tag_rest_pad_h": 8,
    "tags_top3_margin": 8,
    "tags_rest_margin": 4,
    "tags_gap": 8,
}
# 縮放比例上下限：避免檔數極少時字放到誇張大，或極多時縮到不可讀。
# 卡片寬度固定，不像高度能靠檔數自然分配——SCALE_MAX 刻意保守，確保就算檔數
# 很少、垂直方向有很多空間可以放大，兩側固定欄位 + 股名代號 加總寬度仍不會
# 超過卡片可用寬度，不然股名會被擠到觸發 ellipsis 截斷。
SCALE_MIN = 0.65
SCALE_MAX = 1.3


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


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


_SCORE_LOW_RGB = hex_to_rgb(COLOR_ACCENT_DIM)
_SCORE_HIGH_RGB = hex_to_rgb(COLOR_ACCENT_BRIGHT)


def score_color(score, score_min, score_max):
    # 依本次上榜檔數的實際分數區間正規化，而非固定 0~100，
    # 讓深淺差異在分數集中的情況下仍清楚可辨。主題綠色深淺表現高低。
    span = score_max - score_min
    t = 0.5 if span <= 0 else (score - score_min) / span
    t = max(0.0, min(1.0, t))
    r = round(lerp(_SCORE_LOW_RGB[0], _SCORE_HIGH_RGB[0], t))
    g = round(lerp(_SCORE_LOW_RGB[1], _SCORE_HIGH_RGB[1], t))
    b = round(lerp(_SCORE_LOW_RGB[2], _SCORE_HIGH_RGB[2], t))
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    text_color = "#0a0a0a" if luminance > 0.55 else "#f5f5f4"
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


MEDAL_COLORS = {1: COLOR_MEDAL_GOLD, 2: COLOR_MEDAL_SILVER, 3: COLOR_MEDAL_BRONZE}


def build_row_html(rank, entry, score_min, score_max):
    is_top3 = rank <= 3
    tier_class = "row-top3" if is_top3 else "row-rest"

    chg = entry["change_pct"]
    chg_class = "chg-up" if (chg or 0) > 0 else ("chg-down" if (chg or 0) < 0 else "chg-flat")
    score_bg, score_text_color = score_color(
        entry["total_score"], score_min, score_max
    )
    # 狀態圓點：用分數色階（跟分數色塊同一組語彙），高分越接近主題亮綠。
    dot_color = score_bg
    tags = build_tags(entry["dimensions"])

    name = html.escape(entry["name"])
    code = html.escape(entry["code"])

    tag_html = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in tags)

    if is_top3:
        badge_html = (
            f'<div class="badge medal" style="background:{MEDAL_COLORS[rank]};">{rank}</div>'
        )
    else:
        badge_html = f'<div class="badge plain">{rank}</div>'

    return f"""
    <div class="row {tier_class}">
      <div class="row-main">
        <span class="dot" style="background:{dot_color};"></span>
        <div class="badge-col">{badge_html}</div>
        <div class="name-block">
          <span class="name">{name}</span><span class="code">{code}</span>
        </div>
        <div class="close">{fmt_close(entry['close'])}</div>
        <div class="chg {chg_class}">{fmt_pct(chg)}</div>
        <div class="score-block" style="background:{score_bg}; color:{score_text_color};">
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
    background: {COLOR_BG};
    font-family: "Noto Sans CJK TC", "Noto Sans TC", sans-serif;
    color: {COLOR_TEXT_PRIMARY};
    overflow: hidden;
  }}
  .page {{
    width: {CANVAS}px;
    height: {CANVAS}px;
    display: flex;
    flex-direction: column;
    padding: 40px 48px 30px;
  }}

  /* ---- 頂部標題區 ---- */
  .header {{
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
  }}
  .pill {{
    display: inline-block;
    padding: 7px 24px;
    border: 1px solid {COLOR_ACCENT};
    border-radius: 999px;
    color: {COLOR_ACCENT_BRIGHT};
    font-size: 19px;
    font-weight: 600;
    letter-spacing: 2px;
  }}
  .headline {{
    margin-top: 18px;
    font-size: 58px;
    font-weight: 800;
    letter-spacing: 1px;
    color: {COLOR_TEXT_PRIMARY};
    font-variant-numeric: tabular-nums;
  }}
  .subtitle {{
    margin-top: 10px;
    font-size: 21px;
    color: {COLOR_TEXT_SECONDARY};
  }}
  .subtitle b {{
    color: {COLOR_ACCENT_BRIGHT};
    font-weight: 700;
  }}
  .divider {{
    margin-top: 18px;
    width: 72px;
    height: 3px;
    border-radius: 2px;
    background: linear-gradient(90deg, transparent, {COLOR_ACCENT}, transparent);
  }}

  /* ---- 榜單卡片 ---- */
  .card {{
    flex: 1 1 auto;
    min-height: 0;
    margin-top: 20px;
    background: {COLOR_CARD_BG};
    border: 1px solid {COLOR_CARD_BORDER};
    border-radius: 28px;
    padding: 8px 30px 20px;
    display: flex;
    flex-direction: column;
    box-shadow: 0 24px 60px rgba(0,0,0,0.45);
  }}
  .col-header {{
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: {sv['col_gap']:.1f}px;
    padding: 10px 0 8px;
    border-bottom: 1px solid {COLOR_HAIRLINE};
  }}
  .col-header .spacer {{ flex: 1 1 auto; }}
  .col-header .label {{
    flex: 0 0 auto;
    text-align: right;
    font-size: 16px;
    color: {COLOR_TEXT_MUTED};
    letter-spacing: 1px;
  }}
  .col-header .label.close {{ width: {sv['close_col_w']:.1f}px; }}
  .col-header .label.chg {{ width: {sv['chg_col_w']:.1f}px; }}
  .col-header .label.score {{ width: {sv['score_col_w']:.1f}px; }}

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
    gap: {sv['col_gap']:.1f}px;
  }}

  .dot {{
    flex: 0 0 auto;
    width: {sv['dot_size']:.1f}px;
    height: {sv['dot_size']:.1f}px;
    border-radius: 50%;
    margin-right: {sv['dot_gap']:.1f}px;
  }}
  .badge-col {{
    flex: 0 0 auto;
    width: {sv['badge_col_w']:.1f}px;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .badge.plain {{
    font-size: {sv['badge_rest_font']:.1f}px;
    font-weight: 700;
    color: {COLOR_TEXT_MUTED};
    font-variant-numeric: tabular-nums;
  }}
  .badge.medal {{
    width: {sv['badge_top3_size']:.1f}px;
    height: {sv['badge_top3_size']:.1f}px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: {sv['badge_top3_font']:.1f}px;
    font-weight: 800;
    color: #0a0a0a;
    font-variant-numeric: tabular-nums;
    box-shadow: 0 4px 14px rgba(0,0,0,0.4);
  }}

  .name-block {{
    flex: 1 1 auto;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-left: {sv['name_gap']:.1f}px;
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
    width: {sv['close_col_w']:.1f}px;
    text-align: right;
    font-variant-numeric: tabular-nums;
    color: {COLOR_TEXT_SECONDARY};
  }}
  .row-top3 .close {{ font-size: {sv['close_top3_font']:.1f}px; }}
  .row-rest .close {{ font-size: {sv['close_rest_font']:.1f}px; }}

  .chg {{
    flex: 0 0 auto;
    width: {sv['chg_col_w']:.1f}px;
    text-align: center;
    border-radius: 8px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }}
  .row-top3 .chg {{ font-size: {sv['chg_top3_font']:.1f}px; padding: {sv['chg_top3_pad']:.1f}px 0; }}
  .row-rest .chg {{ font-size: {sv['chg_rest_font']:.1f}px; padding: {sv['chg_rest_pad']:.1f}px 0; }}
  /* 台股慣例：漲＝紅、跌＝綠，獨立於主題綠色（跌用的綠色相跟主題teal刻意不同）*/
  .chg-up {{ background: {COLOR_UP_RED_TINT}; color: {COLOR_UP_RED}; }}
  .chg-down {{ background: {COLOR_DOWN_GREEN_TINT}; color: {COLOR_DOWN_GREEN}; }}
  .chg-flat {{ background: rgba(255,255,255,0.06); color: {COLOR_TEXT_SECONDARY}; }}

  .score-block {{
    flex: 0 0 auto;
    width: {sv['score_col_w']:.1f}px;
    text-align: center;
    border-radius: 9px;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
  }}
  .row-top3 .score-block {{ font-size: {sv['score_top3_font']:.1f}px; padding: {sv['score_top3_pad']:.1f}px 0; }}
  .row-rest .score-block {{ font-size: {sv['score_rest_font']:.1f}px; padding: {sv['score_rest_pad']:.1f}px 0; }}

  .tags {{
    display: flex;
    gap: {sv['tags_gap']:.1f}px;
    margin-top: {sv['tags_top3_margin']:.1f}px;
    padding-left: {sv['dot_size'] + sv['dot_gap'] + sv['badge_col_w'] + sv['name_gap'] + 2 * sv['col_gap']:.1f}px;
  }}
  .row-rest .tags {{ margin-top: {sv['tags_rest_margin']:.1f}px; }}
  .tag {{
    background: {COLOR_ACCENT_TINT_BG};
    border: 1px solid {COLOR_ACCENT_TINT_BORDER};
    color: {COLOR_ACCENT_BRIGHT};
    border-radius: 7px;
    padding: {sv['tag_pad_v']:.1f}px {sv['tag_pad_h']:.1f}px;
    white-space: nowrap;
  }}
  .row-top3 .tag {{ font-size: {sv['tag_top3_font']:.1f}px; }}
  .row-rest .tag {{
    font-size: {sv['tag_rest_font']:.1f}px;
    padding: {sv['tag_rest_pad_v']:.1f}px {sv['tag_rest_pad_h']:.1f}px;
  }}

  /* ---- 底部 ---- */
  .footer {{
    flex: 0 0 auto;
    margin-top: 18px;
    display: flex;
    flex-direction: column;
    align-items: center;
  }}
  .footer-mark {{
    width: 46px;
    height: 2px;
    border-radius: 2px;
    background: linear-gradient(90deg, transparent, {COLOR_ACCENT}, transparent);
    margin-bottom: 10px;
  }}
  .footer-text {{
    text-align: center;
    font-size: 16px;
    color: {COLOR_TEXT_MUTED};
  }}
</style>
</head>
<body>
  <div class="page">
    <div class="header">
      <div class="pill">法人吸籌榜</div>
      <div class="headline">{html.escape(data['report_date'])}</div>
      <div class="subtitle">
        通過優質門檻 <b>{data['qualified_count']}</b> 檔　進榜 <b>{n}</b> 檔
      </div>
      <div class="divider"></div>
    </div>

    <div class="card">
      <div class="col-header">
        <div class="spacer"></div>
        <div class="label close">收盤</div>
        <div class="label chg">漲跌</div>
        <div class="label score">吸籌分</div>
      </div>
      <div class="list">
        {rows_html}
      </div>
    </div>

    <div class="footer">
      <div class="footer-mark"></div>
      <div class="footer-text">
        資料來源：TWSE公開資訊，依法人買超／融資／量價／相對強度整理，非投資建議
      </div>
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

        # 第一階段：用 scale=1.0 畫一次，只為了量出榜單卡片內 .list 實際可用高度
        # （header/card/footer 的真實渲染高度不用猜，直接量）。
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
