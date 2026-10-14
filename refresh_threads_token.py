#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
刷新 Threads 長效 access token，並自動把新 token 寫回 GitHub repo 的
THREADS_ACCESS_TOKEN secret，避免每約60天要手動重新走一次換票流程
（這個麻煩本身就是這支腳本存在的原因——2026-10-08 那次過期導致
publish_threads.py 連續失敗好幾天，才處理了半天才搞定）。

背景：Threads 長效 token 效期約60天；Meta 官方刷新端點規定「距離上次
取得/刷新這組 token 至少24小時」才能再刷新一次，但刷新後效期會重新算
60天、沒有次數上限——所以只要每天都跑一次這支腳本，正常情況下 token
理論上永遠不會再過期。

流程：
    1. 用目前還沒過期的 THREADS_ACCESS_TOKEN，呼叫 Meta 官方刷新端點拿新 token。
       （如果距離上次取得/刷新還不滿24小時，Meta 會回錯誤——這是正常現象
       不是真的失敗，這支腳本一樣會以非0結束，但 daily_run.py 那邊會把這
       支腳本的失敗當非致命、只印警告、不影響當天其餘流程，明天會自動再
       重試一次。）
    2. 用 GitHub REST API 讀取這個 repo 的 Actions secrets 公鑰。
    3. 用該公鑰把新 token 做 libsodium sealed box 加密
       （GitHub API 文件規定的加密方式，官方不接受明文）。
    4. 呼叫 GitHub REST API 把加密後的值寫回 THREADS_ACCESS_TOKEN 這個
       secret，蓋掉舊的。
    5. 把新 token 寫到本機一個不會被 git 追蹤、不會被 commit 的暫存檔
       （TOKEN_HANDOFF_PATH），供 daily_run.py 讀取後更新「這次執行」自己
       行程裡的 THREADS_ACCESS_TOKEN 環境變數——Meta 沒有明確保證舊 token
       在刷新後、原訂的60天效期內一定還能繼續用，保守起見，同一次
       daily_run.py 執行若稍後還要呼叫 publish_threads.py，一律改用剛刷新
       出來的這組新 token，不要賭舊的還有效。daily_run.py 讀完這個暫存檔
       後會立刻刪掉，不會留在硬碟上。

權限需求：
    GH_PAT 環境變數——一組對這個 repo 有「Secrets: Read and write」權限的
    fine-grained personal access token。預設 GitHub Actions 自動注入的
    GITHUB_TOKEN 沒有改 Actions secrets 的權限，不能拿來用，一定要另外
    設定一組 PAT。

絕不印出 token 本身（含舊的、新的），只印刷新結果（成功/失敗、新效期天數）。
"""

import base64
import os
import subprocess
import sys
from pathlib import Path

import requests
from nacl import encoding, public

REPO_ROOT = Path(__file__).resolve().parent
GRAPH_API_BASE = "https://graph.threads.net"
GITHUB_API_BASE = "https://api.github.com"
HTTP_TIMEOUT = 30
SECRET_NAME = "THREADS_ACCESS_TOKEN"

# 只在本機磁碟上短暫存在：daily_run.py 用來把新 token 帶回自己的行程環境變數，
# 讀完立刻刪除。不是 git 追蹤的檔案，不會被任何 commit_and_push_if_changed()
# 的 pathspec 帶到（那些都用明確的檔案清單，不會掃到這個檔名）。
TOKEN_HANDOFF_PATH = REPO_ROOT / ".threads_token_refreshed"

# GH_PAT 還沒設定時用這個 exit code，讓 daily_run.py 分得出「功能還沒啟用」
# 跟「有設定但刷新失敗」——前者只印一次提示即可，不用每天都印警告嚇人。
GH_PAT_NOT_CONFIGURED_EXIT_CODE = 3


class RefreshError(Exception):
    """任何一步失敗時丟出，main() 統一接住、印出訊息、以非0結束。"""


def run_git(*args):
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RefreshError(
            f"git {' '.join(args)} 失敗（exit {result.returncode}）：\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout.strip()


def get_repo_owner_and_name():
    # GitHub Actions 執行環境會自動注入 GITHUB_REPOSITORY（格式 owner/repo），
    # 優先用這個；本機手動測試時沒有這個環境變數，退而求其次從 git remote 解析。
    env_repo = os.environ.get("GITHUB_REPOSITORY")
    if env_repo and "/" in env_repo:
        owner, repo = env_repo.split("/", 1)
        return owner, repo

    remote_url = run_git("remote", "get-url", "origin")
    cleaned = remote_url.strip().removesuffix(".git")
    if cleaned.startswith("git@github.com:"):
        owner_repo = cleaned.removeprefix("git@github.com:")
    elif "github.com/" in cleaned:
        owner_repo = cleaned.split("github.com/", 1)[1]
    else:
        raise RefreshError(f"無法從 remote URL 解析 GitHub owner/repo：{remote_url}")
    owner, repo = owner_repo.split("/", 1)
    return owner, repo


def refresh_threads_token(current_token):
    """呼叫 Meta 官方刷新端點，回傳 (新token, expires_in秒)。"""
    try:
        resp = requests.get(
            f"{GRAPH_API_BASE}/refresh_access_token",
            params={"grant_type": "th_refresh_token", "access_token": current_token},
            timeout=HTTP_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        raise RefreshError(f"呼叫 Threads 刷新端點失敗：{e}") from e

    try:
        data = resp.json()
    except ValueError:
        raise RefreshError(
            f"刷新端點回應不是合法 JSON（HTTP {resp.status_code}）：{resp.text[:500]}"
        )

    if resp.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        raise RefreshError(
            f"Threads 刷新端點回傳錯誤（HTTP {resp.status_code}）："
            f"message={err.get('message')} code={err.get('code')} "
            f"error_subcode={err.get('error_subcode')}\n"
            "（常見原因：距離上次取得/刷新這組 token 還不滿24小時，屬正常"
            "現象，明天會自動再試一次，不代表 token 已經失效。）"
        )

    new_token = data.get("access_token")
    expires_in = data.get("expires_in")
    if not new_token or not expires_in:
        raise RefreshError(f"刷新端點回應缺少 access_token 或 expires_in：{data}")
    return new_token, expires_in


def get_secrets_public_key(owner, repo, gh_pat):
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/actions/secrets/public-key"
    try:
        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {gh_pat}",
                "Accept": "application/vnd.github+json",
            },
            timeout=HTTP_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        raise RefreshError(f"取得 repo secrets 公鑰失敗：{e}") from e
    if resp.status_code >= 400:
        raise RefreshError(
            f"取得 repo secrets 公鑰失敗（HTTP {resp.status_code}）：{resp.text[:500]}\n"
            "（常見原因：GH_PAT 沒有這個 repo 的「Secrets: Read and write」"
            "權限，或 PAT 已過期/被撤銷。）"
        )
    try:
        data = resp.json()
        key_id, key = data["key_id"], data["key"]
    except (ValueError, KeyError) as e:
        raise RefreshError(
            f"repo secrets 公鑰回應格式不如預期（HTTP {resp.status_code}）："
            f"{resp.text[:500]}\n（{e!r}）"
        ) from e
    return key_id, key


def encrypt_secret(public_key_b64, secret_value):
    """依 GitHub 文件規定的方式，用 repo 公鑰做 libsodium sealed box 加密。"""
    public_key = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def update_github_secret(owner, repo, gh_pat, secret_name, key_id, encrypted_value):
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/actions/secrets/{secret_name}"
    try:
        resp = requests.put(
            url,
            headers={
                "Authorization": f"Bearer {gh_pat}",
                "Accept": "application/vnd.github+json",
            },
            json={"encrypted_value": encrypted_value, "key_id": key_id},
            timeout=HTTP_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        raise RefreshError(f"更新 GitHub secret {secret_name} 失敗：{e}") from e
    if resp.status_code not in (201, 204):
        raise RefreshError(
            f"更新 GitHub secret {secret_name} 失敗（HTTP {resp.status_code}）："
            f"{resp.text[:500]}"
        )


def main():
    current_token = os.environ.get("THREADS_ACCESS_TOKEN")
    gh_pat = os.environ.get("GH_PAT")

    if not gh_pat:
        print(
            "[尚未設定] 沒有 GH_PAT 環境變數，token 自動刷新功能尚未啟用"
            "（需要一組對這個 repo 有「Secrets: Read and write」權限的"
            "GitHub personal access token，見本檔案開頭說明）。",
            file=sys.stderr,
        )
        sys.exit(GH_PAT_NOT_CONFIGURED_EXIT_CODE)

    if not current_token:
        print("[錯誤] 缺少環境變數：THREADS_ACCESS_TOKEN，無法刷新 token。", file=sys.stderr)
        sys.exit(1)

    try:
        owner, repo = get_repo_owner_and_name()

        new_token, expires_in = refresh_threads_token(current_token)
        expires_days = expires_in / 86400
        print(f"[刷新] 成功拿到新 token，效期約 {expires_days:.1f} 天。", file=sys.stderr)

        key_id, public_key_b64 = get_secrets_public_key(owner, repo, gh_pat)
        encrypted_value = encrypt_secret(public_key_b64, new_token)
        update_github_secret(owner, repo, gh_pat, SECRET_NAME, key_id, encrypted_value)
        print(
            f"[完成] 已把新 token 寫回 GitHub secret {SECRET_NAME}"
            "（下次執行排程就會用到這組新的）。",
            file=sys.stderr,
        )

        # 寫回 GitHub secret 只影響「之後」的排程；這次 daily_run.py 行程裡
        # 如果稍後還會呼叫 publish_threads.py，保守起見也要改用這組新 token
        # （見本檔案開頭說明：不確定舊 token 在刷新後是否立刻失效）。
        TOKEN_HANDOFF_PATH.write_text(new_token, encoding="utf-8")
    except RefreshError as e:
        print(f"\n[刷新失敗]\n{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
