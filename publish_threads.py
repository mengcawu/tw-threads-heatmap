#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
手動發布打通：把 output/leaderboard_card.png + output/caption.txt 發成一則
「單張圖片+文字」的 Threads 貼文，驗證發布路徑。先不排程，手動執行一次。

流程：
    1. 讀 output/leaderboard_card.png、output/caption.txt（不重新產圖/重算，
       只是把既有結果發出去）。
    2. 把 PNG commit + push 到目前這個 git 分支，用「當次 commit 的 SHA」組出
       raw.githubusercontent.com 的公開網址——用 commit SHA 而不是分支名稱，
       是因為分支名稱之後可能被覆蓋/移動，SHA 是不可變的，確保 Threads 那邊
       之後真的抓圖時網址還指向這張圖。若 PNG 內容跟上一個 commit 相比沒有
       變化，就不産生新 commit，直接用目前 HEAD 的 SHA。
    3. 用 Threads Graph API 兩步驟發布：
         a. POST /{user-id}/threads          建立媒體 container（拿 creation_id）
         b. 輪詢 GET /{creation_id}?fields=status 等 container 狀態變成
            FINISHED（官方文件建議的作法，避免 container 還沒處理完就發布失敗）
         c. POST /{user-id}/threads_publish  用 creation_id 正式發布
    4. USER_ID / ACCESS_TOKEN 一律從環境變數讀（THREADS_USER_ID、
       THREADS_ACCESS_TOKEN），絕不寫進程式碼、絕不印出 token 本身。
    5. 每一步的關鍵結果（圖片URL、creation_id、發布後的 post id）印到 stdout。
    6. 任何一步失敗，印出該步驟名稱＋完整錯誤訊息（含 Graph API 回傳的
       error/message/code/fbtrace_id），並以非 0 狀態結束，不吞掉錯誤。

執行方式、環境變數怎麼設定，見本檔案最後或 README 說明；這支腳本本身不會
自動被排程呼叫，需要人工手動執行一次來驗證整條路徑。
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent
IMAGE_PATH = REPO_ROOT / "output" / "leaderboard_card.png"
CAPTION_PATH = REPO_ROOT / "output" / "caption.txt"
IMAGE_REPO_RELATIVE_PATH = "output/leaderboard_card.png"

GRAPH_API_BASE = "https://graph.threads.net/v1.0"
HTTP_TIMEOUT = 30

# container 處理完成前的輪詢設定（官方文件建議 publish 前先確認 status=FINISHED）
CONTAINER_POLL_INTERVAL_SECONDS = 5
CONTAINER_POLL_MAX_ATTEMPTS = 12  # 5秒 * 12 = 最多等 60 秒

# 剛 push 完，raw.githubusercontent.com 的 CDN 有時會落後幾秒才吃到新 commit
# 的內容；建立 container 前先等一下、失敗了再重試幾次，降低 Threads 那邊抓圖
# 抓到 404 或舊內容的機率。
RAW_CDN_INITIAL_DELAY_SECONDS = 5
CREATE_CONTAINER_MAX_ATTEMPTS = 3
CREATE_CONTAINER_RETRY_DELAY_SECONDS = 8


class PublishError(Exception):
    """任何一個發布步驟失敗時丟出，訊息裡已經包含步驟名稱與完整細節。"""


def run_git(*args):
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise PublishError(
            f"git {' '.join(args)} 失敗（exit {result.returncode}）：\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout.strip()


def get_repo_owner_and_name():
    remote_url = run_git("remote", "get-url", "origin")
    # 支援 https://github.com/OWNER/REPO(.git) 或 git@github.com:OWNER/REPO(.git)
    cleaned = remote_url.strip().removesuffix(".git")
    if cleaned.startswith("git@github.com:"):
        owner_repo = cleaned.removeprefix("git@github.com:")
    elif "github.com/" in cleaned:
        owner_repo = cleaned.split("github.com/", 1)[1]
    else:
        raise PublishError(f"無法從 remote URL 解析 GitHub owner/repo：{remote_url}")
    owner, repo = owner_repo.split("/", 1)
    return owner, repo


def commit_and_push_image():
    """把圖片 commit + push，回傳 (commit_sha, branch_name)。沒有變更就用目前 HEAD。"""
    if not IMAGE_PATH.exists():
        raise PublishError(f"找不到圖片檔案：{IMAGE_PATH}")

    branch = run_git("rev-parse", "--abbrev-ref", "HEAD")

    status = run_git("status", "--porcelain", "--", IMAGE_REPO_RELATIVE_PATH)
    if status:
        run_git("add", IMAGE_REPO_RELATIVE_PATH)
        # commit 只帶這個 pathspec：就算 repo 裡當下還有其他檔案被 stage 了
        # （例如使用者手動編輯到一半、還沒 commit 的東西），也不會被這支腳本
        # 的自動 commit 一起帶走、一起 push 出去。
        run_git(
            "commit",
            "-m",
            "發布用：更新 leaderboard_card.png（publish_threads.py 自動 commit）",
            "--",
            IMAGE_REPO_RELATIVE_PATH,
        )
        print("[git] 圖片有變更，已建立新 commit。", file=sys.stderr)
    else:
        print("[git] 圖片內容跟上一個 commit 相同，不建立新 commit。", file=sys.stderr)

    run_git("push", "-u", "origin", branch)
    commit_sha = run_git("rev-parse", "HEAD")
    print(f"[git] 已推送到 origin/{branch}，commit={commit_sha}", file=sys.stderr)
    return commit_sha, branch


def build_raw_url(owner, repo, commit_sha):
    return (
        f"https://raw.githubusercontent.com/{owner}/{repo}/"
        f"{commit_sha}/{IMAGE_REPO_RELATIVE_PATH}"
    )


def graph_api_request(method, url, **kwargs):
    try:
        resp = requests.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
    except requests.exceptions.RequestException as e:
        raise PublishError(f"HTTP 請求失敗：{method} {url}\n{e}") from e

    try:
        data = resp.json()
    except ValueError:
        raise PublishError(
            f"回應不是合法 JSON（HTTP {resp.status_code}）：{method} {url}\n"
            f"原始內容：{resp.text[:2000]}"
        )

    if resp.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        raise PublishError(
            f"Graph API 回傳錯誤（HTTP {resp.status_code}）：{method} {url}\n"
            f"message: {err.get('message')}\n"
            f"type: {err.get('type')}\n"
            f"code: {err.get('code')}\n"
            f"error_subcode: {err.get('error_subcode')}\n"
            f"fbtrace_id: {err.get('fbtrace_id')}\n"
            f"完整回應：{json.dumps(data, ensure_ascii=False)}"
        )
    return data


def create_container(user_id, access_token, image_url, text):
    url = f"{GRAPH_API_BASE}/{user_id}/threads"
    data = graph_api_request(
        "POST",
        url,
        data={
            "media_type": "IMAGE",
            "image_url": image_url,
            "text": text,
            "access_token": access_token,
        },
    )
    creation_id = data.get("id")
    if not creation_id:
        raise PublishError(f"建立 container 沒有回傳 id，完整回應：{data}")
    return creation_id


def create_container_with_retry(user_id, access_token, image_url, text):
    """建立 container，失敗時重試幾次——主要是為了扛 raw CDN 剛 push 完的短暫延遲。"""
    last_err = None
    for attempt in range(1, CREATE_CONTAINER_MAX_ATTEMPTS + 1):
        try:
            return create_container(user_id, access_token, image_url, text)
        except PublishError as e:
            last_err = e
            print(
                f"[container] 第{attempt}次建立失敗，"
                f"{'稍後重試' if attempt < CREATE_CONTAINER_MAX_ATTEMPTS else '不再重試'}：{e}",
                file=sys.stderr,
            )
            if attempt < CREATE_CONTAINER_MAX_ATTEMPTS:
                time.sleep(CREATE_CONTAINER_RETRY_DELAY_SECONDS)
    raise last_err


def wait_until_container_ready(creation_id, access_token):
    """輪詢 container 狀態直到 FINISHED；ERROR 或逾時直接報錯中止。"""
    url = f"{GRAPH_API_BASE}/{creation_id}"
    for attempt in range(1, CONTAINER_POLL_MAX_ATTEMPTS + 1):
        data = graph_api_request(
            "GET",
            url,
            params={"fields": "status,error_message", "access_token": access_token},
        )
        status = data.get("status")
        print(f"[container] 第{attempt}次輪詢，status={status}", file=sys.stderr)
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise PublishError(
                f"Container 處理失敗（status=ERROR）：{data.get('error_message')}"
            )
        time.sleep(CONTAINER_POLL_INTERVAL_SECONDS)
    raise PublishError(
        f"Container 在 {CONTAINER_POLL_MAX_ATTEMPTS * CONTAINER_POLL_INTERVAL_SECONDS} "
        "秒內都沒有變成 FINISHED，中止發布（不強行 publish 未就緒的 container）。"
    )


def publish_container(user_id, access_token, creation_id):
    url = f"{GRAPH_API_BASE}/{user_id}/threads_publish"
    data = graph_api_request(
        "POST",
        url,
        data={"creation_id": creation_id, "access_token": access_token},
    )
    post_id = data.get("id")
    if not post_id:
        raise PublishError(f"發布沒有回傳 id，完整回應：{data}")
    return post_id


def main():
    user_id = os.environ.get("THREADS_USER_ID")
    access_token = os.environ.get("THREADS_ACCESS_TOKEN")
    missing = [
        name
        for name, val in [
            ("THREADS_USER_ID", user_id),
            ("THREADS_ACCESS_TOKEN", access_token),
        ]
        if not val
    ]
    if missing:
        print(
            f"[錯誤] 缺少環境變數：{', '.join(missing)}。"
            "請先設定好再執行，不會用任何預設值/寫死的憑證頂替。",
            file=sys.stderr,
        )
        sys.exit(1)

    if not CAPTION_PATH.exists():
        print(f"[錯誤] 找不到文案檔案：{CAPTION_PATH}", file=sys.stderr)
        sys.exit(1)
    caption_text = CAPTION_PATH.read_text(encoding="utf-8").strip()

    try:
        owner, repo = get_repo_owner_and_name()
        commit_sha, branch = commit_and_push_image()
        image_url = build_raw_url(owner, repo, commit_sha)
        print(f"[圖片URL] {image_url}")

        print(
            f"[git] 等 {RAW_CDN_INITIAL_DELAY_SECONDS} 秒讓 raw CDN 跟上剛推送的內容...",
            file=sys.stderr,
        )
        time.sleep(RAW_CDN_INITIAL_DELAY_SECONDS)

        print("[步驟1] 建立 container...", file=sys.stderr)
        creation_id = create_container_with_retry(
            user_id, access_token, image_url, caption_text
        )
        print(f"[creation_id] {creation_id}")

        print("[步驟2] 等待 container 就緒...", file=sys.stderr)
        wait_until_container_ready(creation_id, access_token)

        print("[步驟3] 正式發布...", file=sys.stderr)
        post_id = publish_container(user_id, access_token, creation_id)
        print(f"[已發布 post id] {post_id}")

    except PublishError as e:
        print(f"\n[發布失敗]\n{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
