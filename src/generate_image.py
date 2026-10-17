import os
import json
import base64
import requests
from discord_notifier import send_to_discord

MAX_IMAGES = int(os.environ.get("MAX_IMAGES", "6"))  # 保険として上限を設ける

def generate_scene_images(image_prompts, output_dir="output"):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("⚠️ GEMINI_API_KEY が設定されていないため、画像の生成をスキップします。")
        send_to_discord("⚠️ **【進捗通知】GEMINI_API_KEY が未設定のため画像生成をスキップしました。**")
        return

    os.makedirs(output_dir, exist_ok=True)
    
    # 💡 Developer API 用の正しい Imagen 3 エンドポイント
    url = f"https://generativelanguage.googleapis.com/v1beta/models/imagen-3.0-generate-002:generateImages?key={api_key}"

    for idx, prompt in enumerate(image_prompts[:MAX_IMAGES], start=1):
        print(f"🎨 Scene {idx} 画像生成中: {prompt[:40]}...")
        
        payload = {
            "prompt": f"{prompt}, 8k resolution, cinematic lighting, vertical 9:16 aspect ratio",
            "config": {
                "numberOfImages": 1,
                "aspectRatio": "9:16",
                "outputMimeType": "image/jpeg"
            }
        }

        try:
            res = requests.post(url, json=payload, headers={"Content-Type": "application/json"})
            if not res.ok:
                print(f"  ⚠️ Scene {idx} APIエラー ({res.status_code}): {res.text}")
                send_to_discord(f"⚠️ **【進捗通知】Scene {idx} の画像生成に失敗しました ({res.status_code})**")
                continue

            data = res.json()
            generated_images = data.get("generatedImages", [])
            if generated_images and "image" in generated_images[0]:
                img_bytes_b64 = generated_images[0]["image"].get("imageBytes")
                if img_bytes_b64:
                    img_bytes = base64.b64decode(img_bytes_b64)
                    image_path = os.path.join(output_dir, f"scene_{idx:02d}.jpg")
                    with open(image_path, "wb") as f:
                        f.write(img_bytes)
                    print(f"  └ 保存完了: {image_path}")

                    # 🖼️ 生成された画像を Discord に送信
                    msg = f"🖼️ **【進捗通知】Scene {idx} の背景画像を生成しました！**\n**プロンプト:** `{prompt}`"
                    send_to_discord(msg, file_path=image_path)
            else:
                print(f"  ⚠️ Scene {idx} 応答データ構造が不正です: {data}")

        except Exception as e:
            print(f"  ⚠️ Scene {idx} 画像生成例外: {e}")

if __name__ == "__main__":
    json_path = "output/trivia_result.json"
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        generate_scene_images(data.get("image_prompts", []))
    else:
        print(f"⚠️ {json_path} が存在しません。")
