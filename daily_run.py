#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日主流程：抓資料 → 交易日判斷 → 清洗併入歷史 → 評分 → 繪圖 → 文案 → 發布。

依序執行既有、已個別驗證過的腳本（不重寫抓取/評分/繪圖/發布邏輯，只負責串起來
＋失敗就停＋落地 commit）：

    1+2+3. python3 ingest_stock_day.py       抓當日全市場量價，清洗只留普通股，
                                              併入 data/stock_day_common.csv，
                                              裁剪維持滾動30個交易日。
           這一步同時是「交易日判斷」：ingest_stock_day.py 對「TWSE 有回應但
           資料是空的」這個情況，會以 exit code 2 結束（跟連線失敗的 exit 1
           分開）。exit 2 時，本流程印出「非交易日，跳過」、正常結束
           （exit 0）——絕不發文、不當成錯誤、不留半成品。判斷依據純粹是
           「TWSE 今天是否回傳資料」，不查任何寫死的假日表。

       python3 backfill_institutional.py    抓三大法人「個股別」買賣超，對齊
                                              上面的交易日視窗，同樣裁剪維持
                                              滾動30天。
       python3 backfill_institutional_flow.py
                                              抓三大法人「全市場總額」買賣金額
                                              統計（新台幣元，不分個股），供
                                              文案的法人資金流向段落使用，同樣
                                              對齊交易日視窗。
       python3 fetch_taiex_history.py       抓大盤加權指數，同樣只涵蓋這個
                                              30天視窗（會先用既有 data/taiex.csv
                                              當快取，通常只需要抓「今天」一筆
                                              新資料，不必每天重抓整個30天）。

       五份 data/*.csv 都成功、且確定不是「非交易日跳過」之後，才 commit+push
       ——就算之後（評分/繪圖/發布）失敗，今天抓到、清洗好的資料也已經落地，
       不用明天重跑一次。

    4+5. python3 render_card.py             內部會重新執行 score_stocks.py
                                              （四維評分 + veto 排除 + 排序），
                                              讀它的 JSON 輸出畫圖卡，同時把
                                              JSON 另存到 output/leaderboard.json，
                                              PNG 存到 output/leaderboard_card.png。

    6. python3 generate_caption.py           讀 leaderboard.json，套純模板產生
                                              Threads 文案，存到 output/caption.txt。

       榜單/圖卡/文案都成功後才 commit+push output/ 這三個檔案。

    7. python3 publish_threads.py            把 docs/leaderboard_card.png 更新、
                                              透過 GitHub Pages 網址＋Threads
                                              Graph API 兩步驟正式發布。
                                              THREADS_USER_ID / THREADS_ACCESS_TOKEN
                                              直接繼承目前行程的環境變數，不在
                                              這裡碰、不印出。

任何一步（除了上面明確講的「非交易日」）非 0 結束，本流程立刻停止、印出
是哪一步失敗＋完整輸出，不繼續往下、不發布任何半成品內容。
"""

import csv
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

NON_TRADING_DAY_EXIT_CODE = 2  # 跟 ingest_stock_day.py 的 exit code 約定一致

DATA_PATHS = [
    "data/stock_day_common.csv",
    "data/institutional.csv",
    "data/institutional_flow.csv",
    "data/margin.csv",
    "data/taiex.csv",
]
OUTPUT_PATHS = [
    "output/leaderboard.json",
    "output/leaderboard_card.png",
    "output/caption.txt",
]


class StepError(Exception):
    """git 操作失敗時丟出；子腳本失敗走 sys.exit，不用例外（見 run_step）。"""


def run_git(*args):
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise StepError(
            f"git {' '.join(args)} 失敗（exit {result.returncode}）：\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout.strip()


def commit_and_push_if_changed(paths, message):
    """只 add+commit 給定的 pathspec，沒變更就不建立空 commit。"""
    status = run_git("status", "--porcelain", "--", *paths)
    if not status:
        print(f"[git] {paths} 無變更，不建立 commit。", flush=True)
        return
    run_git("add", "--", *paths)
    # commit 只帶這些 pathspec：避免把當下 repo 裡其他已 stage 但無關的檔案
    # 一起夾帶 commit+push 出去。
    run_git("commit", "-m", message, "--", *paths)
    branch = run_git("rev-parse", "--abbrev-ref", "HEAD")
    run_git("push", "-u", "origin", branch)
    print(f"[git] 已 commit + push：{message}", flush=True)


def run_step(step_name, script):
    """執行子腳本，即時繼承 stdout/stderr（CI log 看得到即時進度）。回傳 exit code。"""
    print(f"\n===== {step_name}（python3 {script}）=====", flush=True)
    result = subprocess.run([sys.executable, script], cwd=REPO_ROOT)
    return result.returncode


def fail(step_name, returncode):
    print(
        f"\n[中止] 「{step_name}」失敗（exit {returncode}），"
        "不繼續往下、不發布任何半成品內容。",
        file=sys.stderr,
    )
    sys.exit(returncode if returncode != 0 else 1)


def get_latest_trading_date():
    path = REPO_ROOT / "data" / "stock_day_common.csv"
    with open(path, newline="", encoding="utf-8") as f:
        dates = {row["Date"] for row in csv.DictReader(f)}
    if not dates:
        raise StepError("data/stock_day_common.csv 沒有任何資料，無法取得日期。")
    return max(dates)


def main():
    # ---- 步驟 1+2+3：抓量價（含交易日判斷）→ 抓法人 → 抓融資 → 抓大盤指數 ----
    rc = run_step("抓當日量價（含交易日判斷）", "ingest_stock_day.py")
    if rc == NON_TRADING_DAY_EXIT_CODE:
        print("\n非交易日，跳過。", flush=True)
        sys.exit(0)
    if rc != 0:
        fail("抓當日量價", rc)

    rc = run_step("抓三大法人買賣超（個股別）", "backfill_institutional.py")
    if rc != 0:
        fail("抓三大法人買賣超（個股別）", rc)

    rc = run_step("抓三大法人買賣金額（全市場總額）", "backfill_institutional_flow.py")
    if rc != 0:
        fail("抓三大法人買賣金額（全市場總額）", rc)

    rc = run_step("抓融資融券餘額", "backfill_margin.py")
    if rc != 0:
        fail("抓融資融券餘額", rc)

    rc = run_step("抓大盤加權指數", "fetch_taiex_history.py")
    if rc != 0:
        fail("抓大盤加權指數", rc)

    try:
        trading_date = get_latest_trading_date()
        commit_and_push_if_changed(DATA_PATHS, f"每日資料更新：{trading_date}")
    except StepError as e:
        print(f"\n[中止] 資料落地 commit 失敗：{e}", file=sys.stderr)
        sys.exit(1)

    # ---- 步驟 4+5：評分（render_card.py 內部會重跑 score_stocks.py）+ 繪圖 ----
    rc = run_step("評分 + 繪圖", "render_card.py")
    if rc != 0:
        fail("評分 + 繪圖", rc)

    # ---- 步驟 6：產生文案 ----
    rc = run_step("產生文案", "generate_caption.py")
    if rc != 0:
        fail("產生文案", rc)

    try:
        commit_and_push_if_changed(OUTPUT_PATHS, f"每日榜單：{trading_date}")
    except StepError as e:
        print(f"\n[中止] 榜單落地 commit 失敗：{e}", file=sys.stderr)
        sys.exit(1)

    # ---- 步驟 7：發布到 Threads（含 docs/ 圖床更新，見 publish_threads.py）----
    rc = run_step("發布到 Threads", "publish_threads.py")
    if rc != 0:
        fail("發布到 Threads", rc)

    print(f"\n===== {trading_date} 全部完成 =====", flush=True)


if __name__ == "__main__":
    main()
