#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
手動發布打通：把 output/leaderboard_card.png + output/caption.txt 發成一則
「單張圖片+文字」的 Threads 貼文，驗證發布路徑。先不排程，手動執行一次。

圖床改用 GitHub Pages（不是 raw.githubusercontent.com）：因為這個 repo 是私人的，
raw.githubusercontent.com 對外部匿名請求（含 Threads 的伺服器）一律無法存取，
Threads 建 container 時會抓圖失敗（error_subcode 2207052）。改成把要公開的圖
複製一份進 docs/，透過 GitHub Pages（Settings → Pages → Deploy from a branch →
main /docs）發布成公開靜態頁面；只有 docs/ 底下的東西會公開，data/、其餘 *.py、
output/ 都不在 docs/ 裡，不會被 Pages 服務到。

流程：
    1. 讀 output/leaderboard_card.png、output/caption.txt（不重新產圖/重算，
       只是把既有結果發出去）。
    2. 把圖以「內容雜湊」當檔名存進 docs/（例如 docs/leaderboard_card-
       1a2b3c4d5e6f7890.png），舊版本檔案清掉只留最新這份，確保 docs/.nojekyll
       存在（關閉 Jekyll 建置，避免 Pages build 因為找不到 Jekyll 需要的結構
       而失敗——我們只需要純靜態檔案），commit + push。強制要求目前分支是
       PAGES_SOURCE_BRANCH（main）——GitHub Pages 設定成只從 main 的 docs/
       部署，在別的分支跑會 push 到那個分支，但 Pages 網址服務的還是 main 上
       的舊內容，兩邊悄悄對不上；所以分支不對直接報錯，不會假裝發布成功。
    3. 圖片網址是 https://{owner}.github.io/{repo}/leaderboard_card-{雜湊}.png。
       檔名帶內容雜湊、不是固定的 leaderboard_card.png：早期版本靠
       ?v={commit_sha} 這種 query string 當 cache-busting 參數，結果實測
       GitHub Pages 背後的 CDN 對同一個路徑的快取不會因為 query string 不同
       就失效——曾經真的發生「Threads 貼文文字是對的新資料，圖片卻是前一天
       舊圖」的事故。改成內容一變、路徑就換，從根本避免同路徑不同內容的情境。
    4. Pages 部署有延遲，push 完不會馬上生效：建立 container 前先輪詢這個
       URL，確認回應 200、content-type 是 image/*，而且**抓到的內容雜湊真的
       等於剛部署的這份**（不是只信任狀態碼，因為狀態碼可能是 CDN 快取住的
       舊內容也回 200），最多等 120 秒；逾時就直接報錯中止，不會拿可能是舊
       內容的圖硬送給 Threads。
    5. 用 Threads Graph API 兩步驟發布：
         a. POST /{user-id}/threads          建立媒體 container（拿 creation_id）
         b. 輪詢 GET /{creation_id}?fields=status 等 container 狀態變成
            FINISHED（官方文件建議的作法，避免 container 還沒處理完就發布失敗）
         c. 緩衝等待 PUBLISH_BUFFER_AFTER_FINISHED_SECONDS 秒——實測 status
            變成 FINISHED 跟 Threads 後端真的能拿這個 media 發文之間還有時間差，
            立刻發會回 code=24「The media cannot be found」
         d. POST /{user-id}/threads_publish  用 creation_id 正式發布；如果還是
            回 code=24，視為「media 尚未就緒」，隔幾秒重試，最多重試
            PUBLISH_RETRY_MAX_ATTEMPTS 次、間隔遞增；其他錯誤碼不重試，
            直接往外丟
    6. USER_ID / ACCESS_TOKEN 一律從環境變數讀（THREADS_USER_ID、
       THREADS_ACCESS_TOKEN），絕不寫進程式碼、絕不印出 token 本身。
    7. 每一步的關鍵結果（圖片URL、creation_id、發布後的 post id）印到 stdout。
    8. 任何一步失敗，印出該步驟名稱＋完整錯誤訊息（含 Graph API 回傳的
       error/message/code/fbtrace_id），並以非 0 狀態結束，不吞掉錯誤。

執行方式、環境變數怎麼設定，見 README 說明；這支腳本本身不會自動被排程呼叫，
需要人工手動執行一次來驗證整條路徑。
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent

OUTPUT_IMAGE_PATH = REPO_ROOT / "output" / "leaderboard_card.png"
CAPTION_PATH = REPO_ROOT / "output" / "caption.txt"

DOCS_DIR = REPO_ROOT / "docs"
DOCS_NOJEKYLL_PATH = DOCS_DIR / ".nojekyll"
DOCS_NOJEKYLL_REPO_RELATIVE_PATH = "docs/.nojekyll"
# 檔名帶內容雜湊（不是固定的 leaderboard_card.png）：GitHub Pages 背後的 CDN
# 實測會把同一個路徑的舊內容快取住，就算網址加 ?v=commit_sha 這種 query
# string 當 cache-busting 參數也一樣被忽略——同一天測出來的真實案例：Threads
# 貼文文字是對的新資料，圖片卻是前一次部署的舊圖。改成內容變了、檔名就換，
# 從根本避免「同一個路徑、不同時間點內容不同」這種需要仰賴 CDN 正確處理
# cache-busting 的情境。
DOCS_IMAGE_FILENAME_PREFIX = "leaderboard_card-"
DOCS_IMAGE_FILENAME_SUFFIX = ".png"

GRAPH_API_BASE = "https://graph.threads.net/v1.0"
HTTP_TIMEOUT = 30

# GitHub Pages（Settings → Pages → Deploy from a branch）目前設定成只從這個
# 分支的 docs/ 部署；如果在別的分支跑這支腳本，docs/ 會被 commit+push 到那個
# 分支，但 Pages 網址服務的還是 main 上的舊內容，兩者對不上、卻不會報錯——
# 所以在 push 前就先擋掉，不是等發錯了才發現。
PAGES_SOURCE_BRANCH = "main"

# container 處理完成前的輪詢設定（官方文件建議 publish 前先確認 status=FINISHED）
CONTAINER_POLL_INTERVAL_SECONDS = 5
CONTAINER_POLL_MAX_ATTEMPTS = 12  # 5秒 * 12 = 最多等 60 秒

# container 輪詢到 status=FINISHED，跟 Threads 後端真的能拿這個 media 去發文
# 之間還是觀察到有時間差：實測 FINISHED 後立刻 threads_publish 會回
# code=24（error_subcode 4279009）"The media cannot be found"。先等一段緩衝，
# 再加上 threads_publish 本身針對這個特定錯誤碼的重試，兩層一起處理這個
# 時序問題。
PUBLISH_BUFFER_AFTER_FINISHED_SECONDS = 5
PUBLISH_MEDIA_NOT_READY_CODE = 24
PUBLISH_RETRY_MAX_ATTEMPTS = 5  # 第一次嘗試之外，最多再重試這麼多次
PUBLISH_RETRY_BASE_DELAY_SECONDS = 5  # 重試間隔遞增：5, 10, 15, 20, 25 秒

# GitHub Pages 部署延遲：push 完不會馬上生效，建 container 前先輪詢確認圖片
# 網址真的能公開抓到、內容雜湊也對得上。現在檔名本身帶內容雜湊、每次都是
# 全新路徑，理論上不會有 CDN 快取舊內容的問題，但 Pages 本身「部署」（不是
# CDN 快取）還是要等一下才會生效，所以保留輪詢機制，等長一點（120秒）比較
# 保險。
PAGES_POLL_INTERVAL_SECONDS = 5
PAGES_POLL_MAX_ATTEMPTS = 24  # 5秒 * 24 = 最多等 120 秒


class PublishError(Exception):
    """任何一個發布步驟失敗時丟出，訊息裡已經包含步驟名稱與完整細節。"""


class GraphAPIError(PublishError):
    """Graph API 回傳明確的 error 物件時丟出，帶上結構化欄位供呼叫端判斷要不要重試。"""

    def __init__(self, message, *, code=None, error_subcode=None):
        super().__init__(message)
        self.code = code
        self.error_subcode = error_subcode


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


def poll_until_ready(check_fn, interval_seconds, max_attempts, timeout_message):
    """通用輪詢：check_fn(attempt) 回傳 (是否就緒, 說明字串)。

    check_fn 也可以直接丟 PublishError 讓輪詢立刻中止（例如遇到明確的
    ERROR 狀態，不用等到逾時才報錯）。逾時的話用 timeout_message 包成
    PublishError 丟出，訊息帶上最後一次輪詢的說明。
    """
    last_detail = None
    for attempt in range(1, max_attempts + 1):
        ready, detail = check_fn(attempt)
        last_detail = detail
        if ready:
            return
        if attempt < max_attempts:
            time.sleep(interval_seconds)
    raise PublishError(f"{timeout_message}（最後一次結果：{last_detail}）")


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


def build_pages_url(owner, repo, filename):
    return f"https://{owner}.github.io/{repo}/{filename}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_tracked_by_git(relative_path):
    # `git ls-files` 不會因為路徑不存在/沒被追蹤而報錯，只是回傳空字串——
    # 用來判斷等一下要不要把這個路徑放進 `git add` 的 pathspec 清單裡。
    return bool(run_git("ls-files", "--", relative_path))


def sync_docs_image():
    """把 output/leaderboard_card.png 以「內容雜湊檔名」複製進 docs/。

    回傳 (repo相對路徑, 完整檔名, 圖片bytes的sha256全長雜湊)。

    只動 docs/leaderboard_card-*.png 這組檔案跟 docs/.nojekyll，不碰 docs/
    以外任何東西，也不會把 data/、*.py、output/ 其他檔案複製進 docs/。
    每次都用新內容重新算檔名，並清掉 docs/ 裡其他舊的 leaderboard_card-*.png，
    避免每天累積一個新檔案。
    """
    if not OUTPUT_IMAGE_PATH.exists():
        raise PublishError(f"找不到圖片檔案：{OUTPUT_IMAGE_PATH}")

    DOCS_DIR.mkdir(exist_ok=True)

    if not DOCS_NOJEKYLL_PATH.exists():
        DOCS_NOJEKYLL_PATH.touch()
        print("[docs] 建立 docs/.nojekyll", file=sys.stderr)

    image_bytes = OUTPUT_IMAGE_PATH.read_bytes()
    digest = sha256_hex(image_bytes)
    filename = f"{DOCS_IMAGE_FILENAME_PREFIX}{digest[:16]}{DOCS_IMAGE_FILENAME_SUFFIX}"
    docs_image_path = DOCS_DIR / filename

    removed_relative_paths = []
    for old_path in DOCS_DIR.glob(f"{DOCS_IMAGE_FILENAME_PREFIX}*{DOCS_IMAGE_FILENAME_SUFFIX}"):
        if old_path.name == filename:
            continue
        relative_path = f"docs/{old_path.name}"
        # 先判斷 git 有沒有追蹤這個檔案，再刪：如果不是（例如上一次執行在
        # commit 之前就中斷、留下的孤兒檔案），刪掉之後就不能把它的路徑放進
        # `git add` 的 pathspec——對一個 git 從沒認得、硬碟上也已經不存在的
        # 路徑呼叫 `git add` 會直接報錯（pathspec did not match any files），
        # 害這次真正要發布的新圖也一起 commit 失敗。
        tracked = is_tracked_by_git(relative_path)
        try:
            old_path.unlink()
        except OSError as e:
            raise PublishError(f"清除舊版本 docs/{old_path.name} 失敗：{e}") from e
        if tracked:
            removed_relative_paths.append(relative_path)
        print(f"[docs] 清掉舊版本：{relative_path}", file=sys.stderr)

    if docs_image_path.exists() and docs_image_path.read_bytes() == image_bytes:
        print(f"[docs] docs/{filename} 內容已跟 output/ 一致，不覆蓋。", file=sys.stderr)
    else:
        docs_image_path.write_bytes(image_bytes)
        print(f"[docs] 已把 output/leaderboard_card.png 存成 docs/{filename}", file=sys.stderr)

    return f"docs/{filename}", filename, digest, removed_relative_paths


def commit_and_push_docs(new_image_relative_path, removed_relative_paths):
    """commit + push docs/ 底下有變更的檔案（只限這些 pathspec），回傳 commit SHA。

    強制要求目前分支就是 PAGES_SOURCE_BRANCH（GitHub Pages 實際部署的來源分支），
    避免在別的分支跑，docs/ push 到那個分支、但 Pages 網址服務的還是 main 上
    舊內容，兩邊悄悄對不上卻沒有任何錯誤訊息。
    """
    branch = run_git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != PAGES_SOURCE_BRANCH:
        raise PublishError(
            f"目前分支是 {branch!r}，但 GitHub Pages 是設定成從 "
            f"{PAGES_SOURCE_BRANCH!r} 分支的 docs/ 部署。在別的分支跑這支腳本，"
            f"docs/ 會 push 到 {branch!r}，但 Pages 網址服務的仍是 "
            f"{PAGES_SOURCE_BRANCH!r} 上的舊內容——請切到 {PAGES_SOURCE_BRANCH!r} "
            "分支再執行。"
        )

    paths = [new_image_relative_path, DOCS_NOJEKYLL_REPO_RELATIVE_PATH, *removed_relative_paths]
    status = run_git("status", "--porcelain", "--", *paths)
    if status:
        # git add 對已經從硬碟刪掉的路徑會正確 stage 成「刪除」，新檔案跟
        # .nojekyll 則正常 stage 成新增/不變；一次 commit 只帶這組 pathspec，
        # 就算 repo 裡當下還有其他檔案被 stage 了（例如使用者手動編輯到一半、
        # 還沒 commit 的東西），也不會被這支腳本的自動 commit 一起帶走推送。
        run_git("add", "--", *paths)
        run_git(
            "commit",
            "-m",
            f"發布用：更新 {new_image_relative_path}（publish_threads.py 自動 commit）",
            "--",
            *paths,
        )
        print("[git] docs/ 有變更，已建立新 commit。", file=sys.stderr)
    else:
        print("[git] docs/ 內容跟上一個 commit 相同，不建立新 commit。", file=sys.stderr)

    run_git("push", "-u", "origin", branch)
    commit_sha = run_git("rev-parse", "HEAD")
    print(f"[git] 已推送到 origin/{branch}，commit={commit_sha}", file=sys.stderr)
    return commit_sha


def wait_until_pages_ready(image_url, expected_sha256):
    """輪詢 Pages 圖片網址，確認 200、content-type 是 image/*、而且抓到的
    內容雜湊真的等於我們剛部署的那份——只檢查狀態碼會被 CDN 快取住的舊內容
    騙過去（實測發生過：HTTP 200 + image/png，但抓到的是前一天的舊圖），
    所以一定要連內容本身都核對過才算就緒。逾時直接報錯，不送。
    """

    def check(attempt):
        try:
            resp = requests.get(image_url, timeout=HTTP_TIMEOUT)
            content_type = resp.headers.get("Content-Type", "")
            status_ok = resp.status_code == 200 and content_type.startswith("image/")
            actual_sha256 = sha256_hex(resp.content) if status_ok else None
            hash_ok = actual_sha256 == expected_sha256
            ready = status_ok and hash_ok
            if not status_ok:
                detail = f"HTTP {resp.status_code}, Content-Type={content_type!r}"
            elif not hash_ok:
                detail = (
                    f"HTTP 200 但內容雜湊對不上（拿到 {actual_sha256[:12]}…，"
                    f"預期 {expected_sha256[:12]}…），研判是 CDN 還在吃舊快取"
                )
            else:
                detail = "HTTP 200 且內容雜湊相符"
        except requests.exceptions.RequestException as e:
            ready = False
            detail = f"請求例外：{e}"
        print(
            f"[pages] 第{attempt}次輪詢 {image_url} -> {detail}"
            f"{'（就緒）' if ready else ''}",
            file=sys.stderr,
        )
        return ready, detail

    poll_until_ready(
        check,
        PAGES_POLL_INTERVAL_SECONDS,
        PAGES_POLL_MAX_ATTEMPTS,
        f"GitHub Pages 圖片網址在 {PAGES_POLL_MAX_ATTEMPTS * PAGES_POLL_INTERVAL_SECONDS} "
        "秒內都沒有就緒（狀態碼對或內容雜湊對不上），中止發布，不拿可能是舊內容的圖"
        "硬送給 Threads。可能原因：Pages 還沒在 repo Settings 裡設定成從 main /docs "
        f"部署、這是第一次部署還在跑（比後續更新慢）、或 CDN 快取還沒過期。網址：{image_url}",
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
        raise GraphAPIError(
            f"Graph API 回傳錯誤（HTTP {resp.status_code}）：{method} {url}\n"
            f"message: {err.get('message')}\n"
            f"type: {err.get('type')}\n"
            f"code: {err.get('code')}\n"
            f"error_subcode: {err.get('error_subcode')}\n"
            f"fbtrace_id: {err.get('fbtrace_id')}\n"
            f"完整回應：{json.dumps(data, ensure_ascii=False)}",
            code=err.get("code"),
            error_subcode=err.get("error_subcode"),
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


def wait_until_container_ready(creation_id, access_token):
    """輪詢 container 狀態直到 FINISHED；ERROR 或逾時直接報錯中止。"""
    url = f"{GRAPH_API_BASE}/{creation_id}"

    def check(attempt):
        data = graph_api_request(
            "GET",
            url,
            params={"fields": "status,error_message", "access_token": access_token},
        )
        status = data.get("status")
        print(f"[container] 第{attempt}次輪詢，status={status}", file=sys.stderr)
        if status == "ERROR":
            raise PublishError(
                f"Container 處理失敗（status=ERROR）：{data.get('error_message')}"
            )
        return status == "FINISHED", f"status={status}"

    poll_until_ready(
        check,
        CONTAINER_POLL_INTERVAL_SECONDS,
        CONTAINER_POLL_MAX_ATTEMPTS,
        f"Container 在 {CONTAINER_POLL_MAX_ATTEMPTS * CONTAINER_POLL_INTERVAL_SECONDS} "
        "秒內都沒有變成 FINISHED，中止發布（不強行 publish 未就緒的 container）。",
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


def publish_container_with_retry(user_id, access_token, creation_id):
    """發布，遇到 code=24（media 尚未就緒）就重試；其他錯誤照常直接報錯不重試。

    最多嘗試 1 + PUBLISH_RETRY_MAX_ATTEMPTS 次，重試間隔遞增
    （PUBLISH_RETRY_BASE_DELAY_SECONDS 的 1 倍、2 倍、3 倍……）。
    """
    last_err = None
    total_attempts = PUBLISH_RETRY_MAX_ATTEMPTS + 1
    for attempt in range(1, total_attempts + 1):
        try:
            return publish_container(user_id, access_token, creation_id)
        except GraphAPIError as e:
            if e.code != PUBLISH_MEDIA_NOT_READY_CODE:
                raise  # 不是「media 尚未就緒」這類錯誤，不重試，照常往外丟
            last_err = e
            if attempt < total_attempts:
                delay = PUBLISH_RETRY_BASE_DELAY_SECONDS * attempt
                print(
                    f"[publish] 第{attempt}次發布回 code={e.code}"
                    f"（error_subcode={e.error_subcode}），研判是 media 尚未就緒，"
                    f"{delay}秒後重試（還剩{total_attempts - attempt}次機會）...",
                    file=sys.stderr,
                )
                time.sleep(delay)
    print(
        f"[publish] 重試 {PUBLISH_RETRY_MAX_ATTEMPTS} 次後仍是 code="
        f"{PUBLISH_MEDIA_NOT_READY_CODE}，放棄。",
        file=sys.stderr,
    )
    raise last_err


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

        image_relative_path, image_filename, image_sha256, removed_paths = sync_docs_image()
        commit_and_push_docs(image_relative_path, removed_paths)

        image_url = build_pages_url(owner, repo, image_filename)
        print(f"[圖片URL] {image_url}")

        print("[步驟1] 等待 GitHub Pages 部署就緒...", file=sys.stderr)
        wait_until_pages_ready(image_url, image_sha256)

        print("[步驟2] 建立 container...", file=sys.stderr)
        creation_id = create_container(user_id, access_token, image_url, caption_text)
        print(f"[creation_id] {creation_id}")

        print("[步驟3] 等待 container 就緒...", file=sys.stderr)
        wait_until_container_ready(creation_id, access_token)

        print(
            f"[container] status=FINISHED，緩衝等待 "
            f"{PUBLISH_BUFFER_AFTER_FINISHED_SECONDS} 秒再發布...",
            file=sys.stderr,
        )
        time.sleep(PUBLISH_BUFFER_AFTER_FINISHED_SECONDS)

        print("[步驟4] 正式發布...", file=sys.stderr)
        post_id = publish_container_with_retry(user_id, access_token, creation_id)
        print(f"[已發布 post id] {post_id}")

    except PublishError as e:
        print(f"\n[發布失敗]\n{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
