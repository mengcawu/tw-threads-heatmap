#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 output/leaderboard_card.png + output/caption.txt 發布到 Instagram（單張圖片
+ 文字說明的貼文），透過 Meta/Instagram Graph API。

跟 publish_threads.py 共用大部分邏輯（直接 import 復用該模組，不重寫）：
PublishError/GraphAPIError 例外類別、graph_api_request()（Graph API 的錯誤
格式跟 Threads API 相同）、poll_until_ready()、get_repo_owner_and_name()、
build_pages_url()、sha256_hex()、wait_until_pages_ready()。圖片一律重用
publish_threads.py 已經同步到 docs/ 的那一份，不重新上傳、不重新部署——
daily_run.py 保證這支腳本一定接在 publish_threads.py 之後執行，這時候圖片
已經在 GitHub Pages 上線過一次。

流程（Instagram Graph API 官方兩步驟發布）：
    1. 讀 output/caption.txt、output/leaderboard_card.png（不重新產圖/重算）。
    2. 用跟 publish_threads.py 相同的內容雜湊規則，算出 docs/ 裡目前這張圖片
       對應的公開網址，輪詢確認 GitHub Pages 上真的抓得到、內容雜湊也對得上
       （防護理由跟 Threads 版一樣：怕 CDN 還在吃前一次部署的舊快取）。
    3. POST /{ig-user-id}/media                     建立 media container，
                                                     帶 image_url + caption，
                                                     拿 creation_id
    4. 輪詢 GET /{creation_id}?fields=status_code    等 container 狀態變成
                                                     FINISHED（IG 官方文件
                                                     建議的作法）
    5. POST /{ig-user-id}/media_publish              用 creation_id 正式發布，
                                                     拿到貼文的 media id
    6. IG_USER_ID / IG_ACCESS_TOKEN 一律從環境變數讀，絕不寫進程式碼、
       絕不印出 token 本身。
    7. 任何一步失敗，印出該步驟名稱＋完整錯誤訊息（含 Graph API 回傳的
       error/message/code/fbtrace_id），並以非 0 狀態結束，不吞掉錯誤。

IG_USER_ID：Instagram「商業帳號」的數字ID（不是帳號名稱／不是粉專ID），
透過已連接的 Facebook 粉專查 GET /{page-id}?fields=instagram_business_account
可以拿到。IG_ACCESS_TOKEN：需要 instagram_basic、instagram_content_publish、
pages_show_list、pages_read_engagement 權限的長效 access token。申請/設定
步驟見 README。

IG_USER_ID／IG_ACCESS_TOKEN 兩個都還沒設定（尚未申請好 Instagram 串接）時，
視為「這個功能還沒啟用」，以 IG_NOT_CONFIGURED_EXIT_CODE 結束，跟「設定了
但發布失敗」（真的錯誤，exit 1）分開——daily_run.py 據此判斷要不要讓整個
流程回報失敗：還沒設定 IG 之前，Threads 照常每天發文，不會被這支腳本卡住。
只設定其中一個（兩者不同時有值）則視為設定不完整，當成真的錯誤處理。

跟 publish_threads.py 的已知差異：
  - API base 是 graph.facebook.com（Meta Graph API），不是 graph.threads.net。
  - container 狀態欄位是 status_code，不是 status。
  - 目前沒觀察到 Threads 那個「FINISHED 後還要緩衝幾秒才能真的 publish」的
    時序問題，所以沒有加對應的緩衝等待/重試；如果之後實測 Instagram 也有
    類似情況，比照 publish_threads.py 的 PUBLISH_BUFFER_AFTER_FINISHED_SECONDS
    /publish_container_with_retry() 加上去。
"""

import os
import sys

import publish_threads as tp

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"

CONTAINER_POLL_INTERVAL_SECONDS = 5
CONTAINER_POLL_MAX_ATTEMPTS = 12  # 5秒 * 12 = 最多等 60 秒

# IG_USER_ID/IG_ACCESS_TOKEN 兩個都沒設定時用這個 exit code，
# 讓 daily_run.py 判斷成「功能尚未啟用」而不是「發布失敗」。
IG_NOT_CONFIGURED_EXIT_CODE = 3


def get_current_image_url():
    """算出 publish_threads.py 已經部署到 docs/ 的那張圖片的公開網址，不重新
    上傳、不重新部署。回傳 (網址, 圖片bytes的sha256全長雜湊)。"""
    if not tp.OUTPUT_IMAGE_PATH.exists():
        raise tp.PublishError(f"找不到圖片檔案：{tp.OUTPUT_IMAGE_PATH}")
    image_bytes = tp.OUTPUT_IMAGE_PATH.read_bytes()
    digest = tp.sha256_hex(image_bytes)
    filename = f"{tp.DOCS_IMAGE_FILENAME_PREFIX}{digest[:16]}{tp.DOCS_IMAGE_FILENAME_SUFFIX}"
    owner, repo = tp.get_repo_owner_and_name()
    return tp.build_pages_url(owner, repo, filename), digest


def create_container(ig_user_id, access_token, image_url, caption):
    url = f"{GRAPH_API_BASE}/{ig_user_id}/media"
    data = tp.graph_api_request(
        "POST",
        url,
        data={
            "image_url": image_url,
            "caption": caption,
            "access_token": access_token,
        },
    )
    creation_id = data.get("id")
    if not creation_id:
        raise tp.PublishError(f"建立 container 沒有回傳 id，完整回應：{data}")
    return creation_id


def wait_until_container_ready(creation_id, access_token):
    """輪詢 container 狀態直到 FINISHED；ERROR 或逾時直接報錯中止。"""
    url = f"{GRAPH_API_BASE}/{creation_id}"

    def check(attempt):
        data = tp.graph_api_request(
            "GET",
            url,
            params={"fields": "status_code", "access_token": access_token},
        )
        status_code = data.get("status_code")
        print(f"[container] 第{attempt}次輪詢，status_code={status_code}", file=sys.stderr)
        if status_code == "ERROR":
            raise tp.PublishError(f"Container 處理失敗（status_code=ERROR）：{data}")
        return status_code == "FINISHED", f"status_code={status_code}"

    tp.poll_until_ready(
        check,
        CONTAINER_POLL_INTERVAL_SECONDS,
        CONTAINER_POLL_MAX_ATTEMPTS,
        f"Container 在 {CONTAINER_POLL_MAX_ATTEMPTS * CONTAINER_POLL_INTERVAL_SECONDS} "
        "秒內都沒有變成 FINISHED，中止發布（不強行 publish 未就緒的 container）。",
    )


def publish_container(ig_user_id, access_token, creation_id):
    url = f"{GRAPH_API_BASE}/{ig_user_id}/media_publish"
    data = tp.graph_api_request(
        "POST",
        url,
        data={"creation_id": creation_id, "access_token": access_token},
    )
    media_id = data.get("id")
    if not media_id:
        raise tp.PublishError(f"發布沒有回傳 id，完整回應：{data}")
    return media_id


def main():
    ig_user_id = os.environ.get("IG_USER_ID")
    access_token = os.environ.get("IG_ACCESS_TOKEN")
    missing = [
        name
        for name, val in [("IG_USER_ID", ig_user_id), ("IG_ACCESS_TOKEN", access_token)]
        if not val
    ]
    if len(missing) == 2:
        print(
            "[尚未設定] IG_USER_ID、IG_ACCESS_TOKEN 都還沒設定，"
            "視為 Instagram 發布功能尚未啟用，跳過（不算失敗）。",
            file=sys.stderr,
        )
        sys.exit(IG_NOT_CONFIGURED_EXIT_CODE)
    if missing:
        print(
            f"[錯誤] 缺少環境變數：{', '.join(missing)}。"
            "請先設定好再執行，不會用任何預設值/寫死的憑證頂替。",
            file=sys.stderr,
        )
        sys.exit(1)

    if not tp.CAPTION_PATH.exists():
        print(f"[錯誤] 找不到文案檔案：{tp.CAPTION_PATH}", file=sys.stderr)
        sys.exit(1)
    caption_text = tp.CAPTION_PATH.read_text(encoding="utf-8").strip()

    try:
        image_url, image_sha256 = get_current_image_url()
        print(f"[圖片URL] {image_url}")

        print("[步驟1] 確認 GitHub Pages 圖片就緒...", file=sys.stderr)
        tp.wait_until_pages_ready(image_url, image_sha256)

        print("[步驟2] 建立 container...", file=sys.stderr)
        creation_id = create_container(ig_user_id, access_token, image_url, caption_text)
        print(f"[creation_id] {creation_id}")

        print("[步驟3] 等待 container 就緒...", file=sys.stderr)
        wait_until_container_ready(creation_id, access_token)

        print("[步驟4] 正式發布...", file=sys.stderr)
        media_id = publish_container(ig_user_id, access_token, creation_id)
        print(f"[已發布 media id] {media_id}")

    except tp.PublishError as e:
        print(f"\n[發布失敗]\n{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
