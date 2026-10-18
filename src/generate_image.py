import os
import time
from urllib.parse import quote

import requests

from discord_notifier import send_to_discord

MAX_IMAGES = int(os.environ.get("MAX_IMAGES", "6"))  # 保険として上限を設ける

# Pollinations.ai（無料・匿名OKの画像生成API）
# - APIキーなし: image.pollinations.ai（匿名枠。45秒に1枚まで。小さめ水印が付く場合あり）
# - 無料アカウントのキー: gen.pollinations.ai（enter.pollinations.ai で毎日無料クレジットがもらえる。
#   キーがあれば水印なし・間隔制限なし）
POLLINATIONS_TOKEN = os.environ.get("POLLINATIONS_API_KEY", "").strip()
IMAGE_MODEL = os.environ.get("POLLINATIONS_MODEL", "flux")

# 9:16 縦構成（ffmpeg 側で 1080x1920 に合わせて拡大される）
IMG_WIDTH = int(os.environ.get("IMG_WIDTH", "720"))
IMG_HEIGHT = int(os.environ.get("IMG_HEIGHT", "1280"))
PROMPT_SUFFIX = os.environ.get(
    "IMAGE_PROMPT_SUFFIX",
    ", 8k resolution, cinematic lighting, vertical composition, no text, no watermark",
)


def _request_headers():
    if POLLINATIONS_TOKEN:
        return {"Authorization": f"Bearer {POLLINATIONS_TOKEN}"}
    return {}


def _build_url(full_prompt):
    encoded = quote(full_prompt, safe="")
    # nologo=true は匿名枠では無視されることがあるが、付けておいて損はない
    params = f"?model={IMAGE_MODEL}&width={IMG_WIDTH}&height={IMG_HEIGHT}&nologo=true"
    if POLLINATIONS_TOKEN:
        return f"https://gen.pollinations.ai/image/{encoded}{params}", None
    return f"https://image.pollinations.ai/prompt/{encoded}{params}", None


def generate_one_image(prompt, idx, output_dir):
    """1枚生成して保存する。成功時は保存パス、失敗時は例外を投げる。"""
    full_prompt = f"{prompt}{PROMPT_SUFFIX}"
    url, _ = _build_url(full_prompt)
    headers = _request_headers()

    for attempt in range(1, 4):
        try:
            res = requests.get(url, headers=headers, timeout=120)
        except (requests.Timeout, requests.ConnectionError) as e:
            raise RuntimeError(f"接続エラー: {e}")

        if res.ok and "image" in res.headers.get("Content-Type", ""):
            image_path = os.path.join(output_dir, f"scene_{idx:02d}.jpg")
            with open(image_path, "wb") as f:
                f.write(res.content)
            return image_path

        # 匿名枠のレート制限（402/429）。Retry-After に従って待つ
        if res.status_code in (402, 429) and attempt < 3:
            wait = 60
            try:
                wait = min(int(float(res.headers.get("Retry-After", 60))), 90)
            except (TypeError, ValueError):
                pass
            print(f"  ⏳ レート制限({res.status_code})。{wait}秒待って再試行します ({attempt}/3)")
            time.sleep(wait)
            continue

        raise RuntimeError(f"HTTP {res.status_code}: {res.text[:200]}")

    raise RuntimeError("再試行でもレート制限が解消しませんでした")


def generate_scene_images(image_prompts, output_dir="output"):
    os.makedirs(output_dir, exist_ok=True)
    auth_mode = "APIキー" if POLLINATIONS_TOKEN else "匿名(無料)"
    print(f"🎨 Pollinations ({IMAGE_MODEL}, {auth_mode}, {IMG_WIDTH}x{IMG_HEIGHT}) で生成します")

    success = 0
    for idx, prompt in enumerate(image_prompts[:MAX_IMAGES], start=1):
        try:
            print(f"🎨 Scene {idx} 画像生成中: {prompt[:40]}...")
            image_path = generate_one_image(prompt, idx, output_dir)
            success += 1
            print(f"  └ 保存完了: {image_path}")

            msg = (
                f"🖼️ **【進捗通知】Scene {idx} の背景画像を生成しました！**\n"
                f"**プロンプト:** `{prompt}`"
            )
            send_to_discord(msg, file_path=image_path)
        except Exception as e:
            print(f"  ⚠️ Scene {idx} 失敗: {e}")
            send_to_discord(f"⚠️ **【進捗通知】Scene {idx} の画像生成に失敗しました**\n`{str(e)[:200]}`")

        # 匿名枠は「45秒に1枚まで」。間隔を必ず空けて次の生成へ
        if not POLLINATIONS_TOKEN and idx < min(len(image_prompts), MAX_IMAGES):
            time.sleep(48)

    print(f"✅ 画像生成完了: {success} 枚")


if __name__ == "__main__":
    import json

    json_path = "output/trivia_result.json"
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        generate_scene_images(data.get("image_prompts", []))
    else:
        print(f"⚠️ {json_path} が存在しません。")
