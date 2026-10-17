import os

from discord_notifier import send_to_discord

MAX_IMAGES = int(os.environ.get("MAX_IMAGES", "6"))  # 保険として上限を設ける

# 使う Imagen モデル（上から順に試す。環境変数 GEMINI_IMAGE_MODELS で上書き可）
# ※ 旧 imagen-3.0-generate-002 は API から引退済み（404 になる）ため、Imagen 4 系を優先する
IMAGE_MODELS = [
    m.strip() for m in os.environ.get(
        "GEMINI_IMAGE_MODELS",
        "imagen-4.0-generate-001,imagen-4.0-fast-generate-001,imagen-3.0-generate-002",
    ).split(",") if m.strip()
]


def generate_one_image(client, types, model, prompt, idx, output_dir):
    """1枚生成して保存し、保存先パスを返す。失敗時は例外。"""
    full_prompt = f"{prompt}, 8k resolution, cinematic lighting"
    resp = client.models.generate_images(
        model=model,
        prompt=full_prompt,
        config=types.GenerateImagesConfig(
            number_of_images=1,
            aspect_ratio="9:16",
            output_mime_type="image/jpeg",
        ),
    )
    images = resp.generated_images or []
    if not images or not images[0].image or not images[0].image.image_bytes:
        raise RuntimeError("画像データが返されませんでした")

    image_path = os.path.join(output_dir, f"scene_{idx:02d}.jpg")
    with open(image_path, "wb") as f:
        f.write(images[0].image.image_bytes)
    return image_path


def generate_scene_images(image_prompts, output_dir="output"):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("⚠️ GEMINI_API_KEY が設定されていないため、画像の生成をスキップします。")
        send_to_discord("⚠️ **【進捗通知】GEMINI_API_KEY が未設定のため画像生成をスキップしました。**")
        return

    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        print(f"⚠️ google-genai がインストールされていません: {e}")
        send_to_discord("⚠️ **【進捗通知】google-genai パッケージが無いため画像生成をスキップしました。**")
        return

    # 画像生成は時間がかかることがあるためタイムアウトを長めに
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=120_000),
    )
    os.makedirs(output_dir, exist_ok=True)
    print(f"🎨 使用モデル候補: {', '.join(IMAGE_MODELS)}")

    for idx, prompt in enumerate(image_prompts[:MAX_IMAGES], start=1):
        saved_path = None
        last_error = None

        for model in IMAGE_MODELS:
            try:
                print(f"🎨 Scene {idx} 画像生成中 ({model}): {prompt[:40]}...")
                saved_path = generate_one_image(client, types, model, prompt, idx, output_dir)
                print(f"  └ 保存完了: {saved_path} (model={model})")
                break
            except Exception as e:
                last_error = e
                print(f"  ⚠️ {model} 失敗: {e}")
                continue

        if saved_path:
            msg = (
                f"🖼️ **【進捗通知】Scene {idx} の背景画像を生成しました！**\n"
                f"**プロンプト:** `{prompt}`"
            )
            send_to_discord(msg, file_path=saved_path)
        else:
            detail = str(last_error or "原因不明")
            print(f"  ⚠️ Scene {idx} すべてのモデルで失敗: {detail}")
            send_to_discord(
                f"⚠️ **【進捗通知】Scene {idx} の画像生成に失敗しました**\n`{detail[:200]}`"
            )


if __name__ == "__main__":
    import json

    json_path = "output/trivia_result.json"
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        generate_scene_images(data.get("image_prompts", []))
    else:
        print(f"⚠️ {json_path} が存在しません。")
