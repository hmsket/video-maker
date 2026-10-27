"""Discord スレッドから確定台本とメタデータを取得し、output/trivia_result.json を再作成する。

台本の優先順位:
  1. スレッド内で一番新しいコードブロック（ユーザーの修正台本）
  2. 「【生成メタデータ】」メッセージ内の元台本 (script_text) ← 修正投稿がなければこちら
  3. テスト用台本（ MANUAL 実行時などスレッドが読めないとき ）

タイトル・概要・画像/動画プロンプト・出典は fetch_trivia.py が投稿した
「【生成メタデータ】」メッセージの JSON から復元する（無い場合は空で続行）。

必要な環境変数: THREAD_ID, DISCORD_TOKEN
"""

import json
import os
import re

import requests

from discord_notifier import send_to_discord

META_MARKER = "【生成メタデータ】"
PROMPTS_MARKER = "【生成プロンプト】"
API = "https://discord.com/api/v10"
TEST_SCRIPT = "これはテスト用の歴史雑学台本です。正常に音声と画像が生成されるか確認しています。"


def fetch_thread_messages(thread_id, token, limit=50):
    """スレッドのメッセージを新しい順で取得する。失敗時は空リスト。"""
    url = f"{API}/channels/{thread_id}/messages?limit={limit}"
    try:
        res = requests.get(url, headers={"Authorization": f"Bot {token}"}, timeout=30)
        if not res.ok:
            print(f"⚠️ Discord API エラー: {res.status_code} {res.text[:200]}")
            return []
        return res.json()
    except Exception as e:
        print(f"⚠️ Discord 取得例外: {e}")
        return []


def extract_meta(messages):
    """【生成メタデータ】メッセージから JSON を復元する。"""
    for m in messages:
        content = m.get("content", "")
        if META_MARKER not in content:
            continue
        for block in re.findall(r"```(?:json)?\s*\n?([\s\S]*?)```", content):
            try:
                meta = json.loads(block)
            except json.JSONDecodeError:
                continue
            if isinstance(meta, dict) and (meta.get("type") == "video_maker_meta" or "image_prompts" in meta):
                print("✅ メタデータ（プロンプト・出典・元台本）を復元しました。")
                return meta
    print("⚠️ メタデータが見つかりません（古いスレッド、または fetch_trivia.py 実行以前の形式）。")
    return None


def extract_prompt_lists(messages):
    """【生成プロンプト】メッセージから 画像/動画(動き)プロンプト JSON を復元する。"""
    for m in messages:
        content = m.get("content", "")
        if PROMPTS_MARKER not in content:
            continue
        for block in re.findall(r"```(?:json)?\s*\n?([\s\S]*?)```", content):
            try:
                meta = json.loads(block)
            except json.JSONDecodeError:
                continue
            if isinstance(meta, dict) and "image_prompts" in meta:
                print("✅ プロンプトメッセージから画像/動画プロンプトを復元しました。")
                return meta
    return None


def extract_edited_script(messages):
    """メタデータメッセージを除いた中で、一番新しいコードブロック = 修正台本を取り出す。"""
    for m in messages:  # Discord API は新しい順に返す
        content = m.get("content", "")
        if META_MARKER in content:
            continue  # パイプライン内部のメタデータは台本と区別する
        match = re.search(r"```(?:\w+)?\s*\n?([\s\S]*?)```", content)
        if match:
            text = match.group(1).strip()
            if text:
                print("✅ Discord から修正済み台本を抽出しました。")
                return text
    return None


def prepare_trivia():
    os.makedirs("output", exist_ok=True)
    json_path = "output/trivia_result.json"

    if os.path.exists(json_path):
        print(f"ℹ️ {json_path} は既に存在します。")
        return

    thread_id = os.environ.get("THREAD_ID")
    token = os.environ.get("DISCORD_TOKEN")

    messages = []
    if thread_id and token:
        print(f"🔍 Discord Thread ({thread_id}) から台本を取得中...")
        messages = fetch_thread_messages(thread_id, token)
    else:
        print("⚠️ THREAD_ID / DISCORD_TOKEN が未設定のため、Discord から取得しません。")

    meta = extract_meta(messages) if messages else None
    pmeta = extract_prompt_lists(messages) if messages else None
    script_text = extract_edited_script(messages) if messages else ""

    # 30秒シーン構成: 修正台本が {"theme":..., "scenes":[...]} の JSON コードブロックなら
    # scenes として取り込む（/fact の AI 修正もこの JSON の narration を書き換える形で行う）
    scenes = None
    title_from_theme = None
    stripped = (script_text or "").strip()
    if stripped.startswith("{") and '"scenes"' in stripped:
        try:
            payload = json.loads(stripped)
            if isinstance(payload.get("scenes"), list) and payload["scenes"]:
                scenes = payload["scenes"]
                title_from_theme = payload.get("theme")
                script_text = "".join((s.get("narration") or "").strip() for s in scenes)
                print("✅ 修正台本を 30秒シーン構成 (scenes) として取り込みました。")
        except json.JSONDecodeError:
            pass

    # 修正投稿が無い/旧形式の場合はメタデータ側の scenes を採用
    if not scenes and (meta or {}).get("scenes"):
        scenes = meta["scenes"]
        print("ℹ️ メタデータからシーン構成 (scenes) を復元しました。")

    if not script_text and (meta or {}).get("script_text"):
        script_text = meta["script_text"].strip()
        print("ℹ️ 修正投稿がないため、元台本をそのまま使用します。")

    if not script_text:
        print("⚠️ 台本が見つからなかったため、テスト用台本を使用します。")
        script_text = TEST_SCRIPT
        status = "test"
    else:
        status = "confirmed"

    img_prompts, vid_prompts = ((meta or {}).get("image_prompts", []), (meta or {}).get("video_prompts", []))
    if not img_prompts and pmeta:
        img_prompts = pmeta.get("image_prompts", [])
        vid_prompts = pmeta.get("video_motion_prompts", pmeta.get("video_prompts", []))
    if scenes:
        # 台本 JSON（スリム版）にプロンプトが無い場合はプロンプトメッセージ側を採用
        img_prompts = [s.get("image_prompt", "") for s in scenes if s.get("image_prompt")] or img_prompts
        vid_prompts = [s.get("video_motion_prompt", "") for s in scenes if s.get("video_motion_prompt")] or vid_prompts

    data = {
        "thread_id": thread_id,
        "title": title_from_theme or (meta or {}).get("title") or "歴史雑学ショート",
        "summary": (meta or {}).get("summary", ""),
        "script_text": script_text,
        "scenes": scenes or [],
        "sentences": [],
        "image_prompts": img_prompts,
        "video_prompts": vid_prompts,
        "article": (meta or {}).get("article", {}),
        "status": status,
    }

    tmp_path = json_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, json_path)
    print(f"✅ {json_path} を作成・準備しました。")

    # 台本と生成用プロンプトが確定したら投稿
    prompts_formatted = "\n".join(f"- {p}" for p in data["image_prompts"]) or "(画像プロンプトなし)"
    msg = "📝 **【進捗通知】台本・画像プロンプトを準備しました**"
    send_to_discord(msg)
    send_to_discord(prompts_formatted)
    send_to_discord(script_text)


if __name__ == "__main__":
    prepare_trivia()
