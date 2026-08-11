#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取台股當日大盤加權指數與各類股(TWSE官方分類指數)漲跌幅、成交值，印出 JSON 供人工比對。

資料一律來自 twse.com.tw 官方公開資料，不使用任何假資料／估計值：
  - https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX
        大盤加權指數與各類股指數的收盤指數、漲跌點數、漲跌百分比（TWSE 官方分類指數）
  - https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX
        大盤統計資訊（各類成交值加總）、個股當日成交金額
  - https://openapi.twse.com.tw/v1/opendata/t187ap03_L
        上市公司產業別代碼（每家公司對應的 TWSE 產業別代碼）
  - https://openapi.twse.com.tw/v1/opendata/t187ap14_L
        上市公司各產業EPS統計資訊（提供產業別代碼→中文名稱的對照，用來把上面的代碼轉成文字）

各類股「成交值」不是 TWSE 單一欄位直接發布的數字，而是把同一產業別代碼底下所有個股當日
成交金額加總得出（分類依據仍是 TWSE 官方產業別代碼，非人工臆測）。因為 t187ap14_L
僅涵蓋當季已申報 EPS 的公司，並非每個產業別代碼都能對照到中文名稱（例如金融保險等
申報時間較晚的產業），對應不到的類股會標成交值為 null 並附上代碼，不會用猜測的名稱或數字
去補。跨代碼的合併類指數（如「電子工業類指數」「化學生技醫療類指數」等）目前也不計算成交值，
理由相同。

若任何必要來源連不上，腳本會在 stderr 明確列出被擋的網域，並以非 0 狀態結束，
不會印出捏造的資料。
"""

import json
import sys
from datetime import datetime, timedelta

import requests

OPENAPI_BASE = "https://openapi.twse.com.tw/v1"
WWW_BASE = "https://www.twse.com.tw"
TIMEOUT = 20
RETRIES = 3

blocked_hosts = []


def _host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


def fetch_json(url: str, params: dict | None = None, required: bool = True):
    """GET a URL and parse JSON, retrying on transient network errors.

    Returns None (and records the blocked host) if the source is unreachable.
    Never fabricates a substitute value.
    """
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            last_err = e
    host = _host(url)
    print(f"[錯誤] 無法連線至 {host}（{url}）：{last_err}", file=sys.stderr)
    blocked_hosts.append(host)
    if required:
        return None
    return None


def roc_to_iso(roc_date: str) -> str:
    """'1150810' -> '2026-08-10'"""
    year = int(roc_date[:3]) + 1911
    month = roc_date[3:5]
    day = roc_date[5:7]
    return f"{year}-{month}-{day}"


def to_number(s: str):
    if s is None:
        return None
    s = s.replace(",", "").strip()
    if s in ("", "--", "...", "N/A"):
        return None
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return None


def get_all_index_records():
    """TWSE 官方指數清單（大盤 + 各類股指數 + 其他），含收盤指數/漲跌點數/漲跌百分比。"""
    data = fetch_json(f"{OPENAPI_BASE}/exchangeReport/MI_INDEX")
    if not data:
        return None
    return data


def get_market_summary(date_str: str):
    """大盤統計資訊表（成交金額加總）與個股當日成交金額清單。"""
    data = fetch_json(
        f"{WWW_BASE}/rwd/zh/afterTrading/MI_INDEX",
        params={"date": date_str, "type": "ALLBUT0999", "response": "json"},
    )
    if not data:
        return None
    if data.get("stat") != "OK":
        print(f"[錯誤] {date_str} 無成交資料（{data.get('stat')}），可能非交易日。", file=sys.stderr)
        return None
    return data


def get_company_industry_codes():
    """公司代號 -> TWSE 產業別代碼（數字）。"""
    data = fetch_json(f"{OPENAPI_BASE}/opendata/t187ap03_L", required=False)
    if not data:
        return {}
    return {row["公司代號"]: row["產業別"] for row in data if row.get("公司代號")}


def get_industry_code_names(company_code_to_industry: dict):
    """TWSE 產業別代碼 -> 中文名稱，透過與 EPS 統計資料 join 出來（僅涵蓋已對照到的代碼）。"""
    data = fetch_json(f"{OPENAPI_BASE}/opendata/t187ap14_L", required=False)
    if not data:
        return {}
    code_to_name = {}
    for row in data:
        comp = row.get("公司代號")
        name = row.get("產業別")
        code = company_code_to_industry.get(comp)
        if comp and name and code:
            code_to_name.setdefault(code, name)
    return code_to_name


def normalize_sector_label(s: str) -> str:
    """去掉常見後綴，讓指數簡稱與公司產業別文字可以互相比對，例如
    '食品類指數' -> '食品'，'食品工業' -> '食品'。"""
    for suf in ("類指數", "類股", "工業", "業", "類"):
        if s.endswith(suf):
            return s[: -len(suf)]
    return s


def build_sector_turnover(market_summary: dict, company_to_code: dict, code_to_name: dict):
    """把個股成交金額依 TWSE 產業別代碼加總，回傳 {去後綴的產業簡稱: 成交值(元)}。"""
    stock_table = None
    for t in market_summary.get("tables", []):
        if t.get("title") and "每日收盤行情" in t["title"]:
            stock_table = t
            break
    if not stock_table or not stock_table.get("data"):
        return {}

    fields = stock_table["fields"]
    idx_code = fields.index("證券代號")
    idx_amount = fields.index("成交金額")

    turnover_by_code = {}
    for row in stock_table["data"]:
        stock_code = row[idx_code]
        industry_code = company_to_code.get(stock_code)
        if not industry_code:
            continue
        amount = to_number(row[idx_amount])
        if amount is None:
            continue
        turnover_by_code[industry_code] = turnover_by_code.get(industry_code, 0) + amount

    turnover_by_label = {}
    for code, amount in turnover_by_code.items():
        name = code_to_name.get(code)
        if not name:
            continue
        label = normalize_sector_label(name)
        turnover_by_label[label] = turnover_by_label.get(label, 0) + amount
    return turnover_by_label


def build_market_stats(index_records: list, market_summary: dict):
    weighted = next(
        (r for r in index_records if r.get("指數") == "發行量加權股價指數"), None
    )
    if not weighted:
        print("[錯誤] 找不到「發行量加權股價指數」資料。", file=sys.stderr)
        return None

    # TWSE 原始資料：漲跌點數一律為正值大小，正負號要另外看「漲跌」欄位；
    # 漲跌百分比則已經自帶正負號，不能再乘一次符號。
    sign = -1 if weighted.get("漲跌") == "-" else 1
    change_points = to_number(weighted.get("漲跌點數"))
    change_points = None if change_points is None else round(sign * change_points, 2)
    change_percent = to_number(weighted.get("漲跌百分比"))

    turnover_breakdown = {}
    total_all = None
    total_securities = None
    common_stock = None
    for t in market_summary.get("tables", []):
        if t.get("title") and "大盤統計資訊" in t["title"]:
            for row in t["data"]:
                label, amount_str = row[0], row[1]
                amount = to_number(amount_str)
                turnover_breakdown[label] = amount
                if label.startswith("總計"):
                    total_all = amount
                elif label.startswith("證券合計"):
                    total_securities = amount
                elif "一般股票" in label:
                    common_stock = amount
            break

    return {
        "date": roc_to_iso(weighted["日期"]),
        "close": to_number(weighted.get("收盤指數")),
        "change_points": change_points,
        "change_percent": change_percent,
        "turnover_total_all_instruments_twd": total_all,
        "turnover_listed_securities_twd": total_securities,
        "turnover_common_stock_twd": common_stock,
        "turnover_breakdown_twd": turnover_breakdown,
    }


def build_sectors(index_records: list, sector_turnover: dict):
    sectors = []
    for r in index_records:
        name = r.get("指數", "")
        if not name.endswith("類指數"):
            continue
        label = normalize_sector_label(name)
        sign = -1 if r.get("漲跌") == "-" else 1
        change_points = to_number(r.get("漲跌點數"))
        change_points = None if change_points is None else round(sign * change_points, 2)
        change_percent = to_number(r.get("漲跌百分比"))
        turnover = sector_turnover.get(label)
        sectors.append(
            {
                "sector": label,
                "index_name": name,
                "close": to_number(r.get("收盤指數")),
                "change_points": change_points,
                "change_percent": change_percent,
                "turnover_twd": turnover,
                "turnover_note": None
                if turnover is not None
                else "無法從現有官方資料可靠對應個股產業別代碼，未計算成交值加總",
            }
        )
    return sectors


def main():
    index_records = get_all_index_records()
    if not index_records:
        print("[失敗] 無法取得 TWSE 指數資料，中止。", file=sys.stderr)
        report_blocked_and_exit()

    weighted = next(
        (r for r in index_records if r.get("指數") == "發行量加權股價指數"), None
    )
    if not weighted:
        print("[失敗] 指數清單中找不到發行量加權股價指數。", file=sys.stderr)
        report_blocked_and_exit()

    date_str = roc_to_iso(weighted["日期"]).replace("-", "")

    market_summary = get_market_summary(date_str)
    if not market_summary:
        print("[失敗] 無法取得大盤統計資訊／個股成交資料，中止。", file=sys.stderr)
        report_blocked_and_exit()

    company_to_code = get_company_industry_codes()
    code_to_name = get_industry_code_names(company_to_code)
    sector_turnover = build_sector_turnover(market_summary, company_to_code, code_to_name)

    market = build_market_stats(index_records, market_summary)
    sectors = build_sectors(index_records, sector_turnover)

    result = {
        "source": "twse.com.tw (openapi.twse.com.tw + www.twse.com.tw 官方公開資料)",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "market": market,
        "sectors": sectors,
        "blocked_hosts": sorted(set(blocked_hosts)),
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))

    if blocked_hosts:
        print(
            f"[警告] 以下網域連線失敗，部分資料可能缺漏：{sorted(set(blocked_hosts))}",
            file=sys.stderr,
        )


def report_blocked_and_exit():
    if blocked_hosts:
        print(f"[被擋網域] {sorted(set(blocked_hosts))}", file=sys.stderr)
    else:
        print("[提示] 連線成功但 TWSE 回傳的資料不如預期，可能是非交易日或資料尚未發布。", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
