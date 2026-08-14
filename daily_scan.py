"""
台股 EMA20 五條鐵律 - 每日全市場掃描
資料來源：TWSE OpenAPI（上市）+ TPEX OpenAPI（上櫃）
用法：
    python daily_scan.py            # 抓今日資料、寫入DB、產生訊號、寄email
    python daily_scan.py --no-mail  # 只抓資料+算訊號，不寄信（本地測試用）
"""
import os
import sqlite3
import smtplib
import argparse
import datetime as dt
from email.mime.text import MIMEText

import requests
import pandas as pd

# ========== 假設值 / 可調參數（你的截圖沒給明確數字，需要你確認）==========
CONSOLIDATION_LOOKBACK = 20      # 判斷「橫盤」的觀察天數（假設）
CONSOLIDATION_RANGE_PCT = 0.08   # 橫盤定義：過去N日振幅 < 8%（假設，需你確認）
BREAKOUT_GAIN_PCT = 0.05         # 規則1：單日漲幅 > 5%（截圖明確給的數字）
BIAS_THRESHOLD = 0.08            # 規則3：乖離率 > 8% 建議減碼一半（假設，需你調整）
CONSECUTIVE_DAYS_BELOW = 3       # 規則5：連續3日站不回（截圖明確給的數字）
EMA_SPAN = 20

DB_PATH = os.environ.get("TW_SCAN_DB", "tw_daily.db")

# ========== 1. 資料抓取 ==========

def fetch_twse_today() -> pd.DataFrame:
    """抓上市今日全市場收盤價"""
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    df = pd.DataFrame(data)
    # 欄位：Code, Name, ClosingPrice ...（若證交所調整欄位名，這裡要跟著改）
    df = df.rename(columns={"Code": "stock_id", "ClosingPrice": "close"})
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["market"] = "TWSE"
    return df[["stock_id", "close", "market"]].dropna()


def _roc_date(d: dt.date) -> str:
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def fetch_tpex_today(target_date: dt.date) -> pd.DataFrame:
    """抓上櫃今日全市場收盤價"""
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
    params = {"l": "zh-tw", "d": _roc_date(target_date), "s": "0,asc,0"}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data:
        return pd.DataFrame(columns=["stock_id", "close", "market"])
    df = pd.DataFrame(data)
    # TPEX欄位名可能是 SecuritiesCompanyCode / Close，若不同請依實際回應調整
    code_col = "SecuritiesCompanyCode" if "SecuritiesCompanyCode" in df.columns else df.columns[0]
    close_col = "Close" if "Close" in df.columns else [c for c in df.columns if "close" in c.lower()][0]
    df = df.rename(columns={code_col: "stock_id", close_col: "close"})
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["market"] = "TPEX"
    return df[["stock_id", "close", "market"]].dropna()


# ========== 2. 寫入本地DB ==========

def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily (
            date TEXT, stock_id TEXT, market TEXT, close REAL,
            PRIMARY KEY (date, stock_id)
        )
    """)


def save_today(conn, df: pd.DataFrame, date_str: str):
    df = df.copy()
    df["date"] = date_str
    df.to_sql("daily_tmp", conn, if_exists="replace", index=False)
    conn.execute("""
        INSERT OR REPLACE INTO daily (date, stock_id, market, close)
        SELECT date, stock_id, market, close FROM daily_tmp
    """)
    conn.commit()


# ========== 3. 訊號運算 ==========

def compute_signals(conn) -> dict:
    hist = pd.read_sql("SELECT * FROM daily ORDER BY date", conn)
    if hist.empty:
        return {}

    results = {"rule2_watch": [], "rule3_trim_bias": [], "rule4_break_ema": [], "rule5_full_exit": [], "rule1_avoid": []}

    for stock_id, g in hist.groupby("stock_id"):
        g = g.sort_values("date").reset_index(drop=True)
        if len(g) < 5:
            continue  # 資料太少無法判斷

        g["ema20"] = g["close"].ewm(span=EMA_SPAN, adjust=False).mean()
        g["ret"] = g["close"].pct_change()
        g["bias"] = (g["close"] - g["ema20"]) / g["ema20"]
        g["below_ema"] = g["close"] < g["ema20"]

        last = g.iloc[-1]
        name = last["stock_id"]

        # 規則1：橫盤後單日大漲 -> 不參與（排除用途，這裡列出來給你知道被排除的標的）
        if len(g) >= CONSOLIDATION_LOOKBACK:
            window = g["close"].iloc[-CONSOLIDATION_LOOKBACK:-1]
            rng = (window.max() - window.min()) / window.min() if window.min() > 0 else 999
            if rng < CONSOLIDATION_RANGE_PCT and last["ret"] > BREAKOUT_GAIN_PCT:
                results["rule1_avoid"].append(name)
                continue  # 命中規則1直接排除，不進其他規則判斷

        # 規則2：大陽線後回踩EMA20不破 -> 關注
        if len(g) >= 3:
            prev_big_candle = (g["ret"].iloc[-3:-1] > BREAKOUT_GAIN_PCT).any()
            near_ema = abs(last["bias"]) < 0.02  # 觸及EMA20的容忍區間（假設2%）
            if prev_big_candle and near_ema and last["close"] >= last["ema20"]:
                results["rule2_watch"].append(name)

        # 規則3：乖離過大 -> 建議賣一半
        if last["bias"] > BIAS_THRESHOLD:
            results["rule3_trim_bias"].append((name, round(last["bias"] * 100, 1)))

        # 規則4：收盤跌破EMA20 -> 建議賣一半
        if last["below_ema"] and len(g) >= 2 and not g["below_ema"].iloc[-2]:
            results["rule4_break_ema"].append(name)

        # 規則5：連續N日站不回 -> 建議清倉
        if len(g) >= CONSECUTIVE_DAYS_BELOW:
            if g["below_ema"].iloc[-CONSECUTIVE_DAYS_BELOW:].all():
                results["rule5_full_exit"].append(name)

    return results


# ========== 4. Email 通知 ==========

def send_mail(results: dict, date_str: str):
    smtp_user = os.environ["GMAIL_USER"]
    smtp_pass = os.environ["GMAIL_APP_PASSWORD"]
    to_addr = os.environ.get("MAIL_TO", smtp_user)

    lines = [f"【EMA20五律 每日掃描】{date_str}", ""]
    labels = {
        "rule5_full_exit": "🔴 規則5 連續站不回，建議清倉",
        "rule4_break_ema": "🟠 規則4 今日跌破EMA20，建議減半",
        "rule3_trim_bias": "🟡 規則3 乖離過大，建議先賣一半",
        "rule2_watch": "🟢 規則2 回踩不破，列入關注",
        "rule1_avoid": "⚪ 規則1 橫盤後急拉，不參與",
    }
    for key, label in labels.items():
        items = results.get(key, [])
        lines.append(f"{label}（{len(items)}檔）")
        if not items:
            lines.append("  無")
        else:
            for it in items[:50]:  # 避免信件過長
                lines.append(f"  {it}")
        lines.append("")

    body = "\n".join(lines)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"台股EMA20訊號 {date_str}"
    msg["From"] = smtp_user
    msg["To"] = to_addr

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [to_addr], msg.as_string())


# ========== main ==========

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-mail", action="store_true")
    args = parser.parse_args()

    today = dt.date.today()
    date_str = today.strftime("%Y-%m-%d")

    twse = fetch_twse_today()
    tpex = fetch_tpex_today(today)
    all_df = pd.concat([twse, tpex], ignore_index=True)

    if all_df.empty:
        print("今日無資料（可能非交易日），略過。")
        return

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    save_today(conn, all_df, date_str)

    results = compute_signals(conn)
    conn.close()

    print(f"抓取 {len(all_df)} 檔，訊號統計：")
    for k, v in results.items():
        print(f"  {k}: {len(v)}")

    if not args.no_mail:
        send_mail(results, date_str)


if __name__ == "__main__":
    main()
