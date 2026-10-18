import os
import re

import requests

# Discord スレッドに直接添付された動画/画像ファイルをダウンロードして
# 動画合成の素材にする。GitHub へのアップロードは不要。
#
# 使い方（スレッド内に添付）:
#   動画クリップ: clip_01.mp4, clip_02.mp4 ... （番号順に連結される）
#   静止画:       scene_01.jpg ...
# 番号はファイル名から読む。無い場合は「古い投稿から順に」採番する。
# main 生成物（draft_output.mp4 等のボット投稿添付）は除外する。

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
THREAD_ID = (os.environ.get("THREAD_ID") or "").strip()

VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def list_thread_messages_oldest_first(thread_id):
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    url = f"https://discord.com/api/v10/channels/{thread_id}/messages?limit=50"
    res = requests.get(url, headers=headers, timeout=30)
    if not res.ok:
        raise RuntimeError(f"メッセージ取得失敗 HTTP {res.status_code}: {res.text[:200]}")
    # Discord API は新しい順に返す → 古い順に反転
    return list(reversed(res.json()))


def sort_index(att_name, fallback):
    """ファイル名に含まれる数字を優先し、無ければ投稿順で採番する。"""
    base = os.path.splitext(att_name)[0]
    m = re.search(r"(\d+)", base)
    if m:
        return int(m.group(1))
    return fallback


def download(url, dest_path):
    res = requests.get(url, timeout=120, stream=True)
    if not res.ok:
        print(f"  ⚠️ ダウンロード失敗 HTTP {res.status_code}: {dest_path}")
        return False
    with open(dest_path, "wb") as f:
        for chunk in res.iter_content(chunk_size=1024 * 512):
            f.write(chunk)
    return True


def fetch_assets(output_dir="output"):
    clips_dir = os.path.join(output_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    messages = list_thread_messages_oldest_first(THREAD_ID)
    video_atts = []
    image_atts = []

    for pos, msg in enumerate(messages, start=1):
        for att in msg.get("attachments", []):
            name = att.get("filename", "")
            url = att.get("url", "")
            ext = os.path.splitext(name)[1].lower()
            if not url or not ext:
                continue
            is_bot = bool(msg.get("author", {}).get("bot"))
            base_name = os.path.splitext(name)[0].lower()
            # ボット投稿の添付（過去の draft_output.zip など）は素材にならない
            if is_bot:
                continue
            if ext in VIDEO_EXTS:
                video_atts.append((sort_index(name, pos), ext, name, url))
            elif ext in IMAGE_EXTS and (base_name.startswith("scene") or base_name.startswith("clip")):
                image_atts.append((sort_index(name, pos), ext, name, url))

    if not video_atts and not image_atts:
        print("📭 スレッド内にユーザー添付の素材ファイルはありませんでした（自動画像生成へフォールバック）")
        return False

    got_clip, got_image = 0, 0
    for idx, ext, name, url in sorted(video_atts):
        dest = os.path.join(clips_dir, f"clip_{idx:02d}{ext}")
        if download(url, dest):
            print(f"🎬 クリップ取得: {name} → {dest}")
            got_clip += 1

    for idx, ext, name, url in sorted(image_atts):
        dest = os.path.join(output_dir, f"scene_{idx:02d}{ext}")
        if download(url, dest):
            print(f"🖼️ 画像取得: {name} → {dest}")
            got_image += 1

    print(f"✅ Discord 素材取得完了: クリップ {got_clip} 本 / 画像 {got_image} 枚")
    return got_clip > 0


if __name__ == "__main__":
    if not THREAD_ID:
        print("ℹ️ THREAD_ID が指定されていないため、Discord からの素材取得をスキップします")
        raise SystemExit(0)
    if not DISCORD_TOKEN:
        print("⚠️ DISCORD_TOKEN が無いため、Discord からの素材取得をスキップします")
        raise SystemExit(0)
    fetch_assets()
