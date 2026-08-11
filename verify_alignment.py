#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三表對齊驗證：以 data/stock_day_common.csv 的交易日範圍為基準，檢查
data/institutional.csv（三大法人）與 data/margin.csv（融資融券）是否
Code+Date 對得上、有無大量缺漏。只做驗證與報告，不評分、不繪圖、不發布。

輸出：
  1. 三份檔案各自的交易日數、總列數
  2. 隨機抽 3 檔個股，印出它們在三份資料裡同一天（該檔在三表都有資料的最後
     一個交易日）的實際數值，供人工核對 Code+Date 是否對得起來
  3. 對齊後有多少檔「三表齊全」：以每檔個股在 stock_day_common 出現的交易日
     集合為基準，institutional / margin 在同一組交易日都有資料，才算齊全
     （不是只看「有無出現」，而是逐日集合是否一致，避免漏掉中間缺漏的假象）
"""

import csv
import os
import random

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(REPO_ROOT, "data")

PRICE_PATH = os.path.join(DATA_DIR, "stock_day_common.csv")
INST_PATH = os.path.join(DATA_DIR, "institutional.csv")
MARGIN_PATH = os.path.join(DATA_DIR, "margin.csv")


def load(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def by_code_dates(rows):
    m = {}
    for r in rows:
        m.setdefault(r["Code"], set()).add(r["Date"])
    return m


def by_code_date_row(rows):
    m = {}
    for r in rows:
        m[(r["Code"], r["Date"])] = r
    return m


def main():
    price_rows = load(PRICE_PATH)
    inst_rows = load(INST_PATH)
    margin_rows = load(MARGIN_PATH)

    price_days = sorted({r["Date"] for r in price_rows})
    inst_days = sorted({r["Date"] for r in inst_rows})
    margin_days = sorted({r["Date"] for r in margin_rows})

    print("========== 涵蓋範圍 ==========")
    print(f"stock_day_common.csv：{len(price_days)} 個交易日"
          f"（{price_days[0]}~{price_days[-1]}），總列數 {len(price_rows)}" if price_days else "stock_day_common.csv：無資料")
    print(f"institutional.csv：{len(inst_days)} 個交易日"
          f"（{inst_days[0]}~{inst_days[-1]}），總列數 {len(inst_rows)}" if inst_days else "institutional.csv：無資料")
    print(f"margin.csv：{len(margin_days)} 個交易日"
          f"（{margin_days[0]}~{margin_days[-1]}），總列數 {len(margin_rows)}" if margin_days else "margin.csv：無資料")

    price_by_code = by_code_dates(price_rows)
    inst_by_code = by_code_dates(inst_rows)
    margin_by_code = by_code_dates(margin_rows)

    inst_lookup = by_code_date_row(inst_rows)
    margin_lookup = by_code_date_row(margin_rows)
    price_lookup = by_code_date_row(price_rows)

    # 三表齊全：該檔在 stock_day_common 出現的交易日集合，
    # 在 institutional / margin 裡「完全相同」（無缺、無多）才算齊全
    complete_codes = []
    partial_codes = []
    for code, pdays in price_by_code.items():
        idays = inst_by_code.get(code, set())
        mdays = margin_by_code.get(code, set())
        if idays == pdays and mdays == pdays:
            complete_codes.append(code)
        else:
            partial_codes.append((code, pdays, idays, mdays))

    print()
    print("========== 對齊結果 ==========")
    print(f"stock_day_common 個股總數：{len(price_by_code)}")
    print(f"三表齊全（institutional 與 margin 逐日皆與量價一致）：{len(complete_codes)} 檔")
    print(f"三表不齊全（至少一表有缺漏或多出的交易日）：{len(partial_codes)} 檔")

    if partial_codes:
        print()
        print("不齊全樣本（最多列 5 檔，說明缺漏內容）：")
        for code, pdays, idays, mdays in partial_codes[:5]:
            missing_in_inst = sorted(pdays - idays)
            extra_in_inst = sorted(idays - pdays)
            missing_in_margin = sorted(pdays - mdays)
            extra_in_margin = sorted(mdays - pdays)
            print(f"  {code}: 量價 {len(pdays)} 天；"
                  f"institutional 缺 {len(missing_in_inst)} 天{missing_in_inst[:3]}"
                  f"{'...' if len(missing_in_inst) > 3 else ''}"
                  f"{'（多出 ' + str(extra_in_inst) + '）' if extra_in_inst else ''}；"
                  f"margin 缺 {len(missing_in_margin)} 天{missing_in_margin[:3]}"
                  f"{'...' if len(missing_in_margin) > 3 else ''}"
                  f"{'（多出 ' + str(extra_in_margin) + '）' if extra_in_margin else ''}")

    print()
    print("========== 隨機抽樣 3 檔，同一天三表數值核對 ==========")
    if not complete_codes:
        print("沒有三表皆齊全的個股可供抽樣。")
    else:
        random.seed()
        sample = random.sample(complete_codes, min(3, len(complete_codes)))
        for code in sample:
            common_days = sorted(price_by_code[code] & inst_by_code[code] & margin_by_code[code])
            check_date = common_days[-1]
            p = price_lookup.get((code, check_date))
            i = inst_lookup.get((code, check_date))
            m = margin_lookup.get((code, check_date))
            name = p["Name"] if p else (i["Name"] if i else "")
            print(f"\n個股 {code} {name}　日期 {check_date}")
            if p:
                print(f"  量價：Close={p['Close']}  Volume={p['Volume']}  Value={p['Value']}")
            else:
                print("  量價：無資料")
            if i:
                print(f"  法人：ForeignNet={i['ForeignNet']}  TrustNet={i['TrustNet']}  "
                      f"DealerNet={i['DealerNet']}  Total={i['Total']}")
            else:
                print("  法人：無資料")
            if m:
                print(f"  融資融券：MarginBalance={m['MarginBalance']}  "
                      f"MarginChange={m['MarginChange']}  ShortBalance={m['ShortBalance']}")
            else:
                print("  融資融券：無資料")


if __name__ == "__main__":
    main()
