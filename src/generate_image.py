import os
import json
import base64
import requests

def generate_scene_images(image_prompts, output_dir="output"):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("⚠️ GEMINI_API_KEY が設定されていないため、画像の生成をスキップします。")
        return

    os.makedirs(output_dir, exist_ok=True)
    
    # Imagen 3 (Developer API エンドポイント)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/imagen-3.0-generate-002:predict?key={api_key}"

    for idx, prompt in enumerate(image_prompts[:3], start=1):
        print(f"🎨 Scene {idx} 画像生成中: {prompt[:40]}...")
        
        payload = {
            "instances": [
                {"prompt": f"{prompt}, 8k resolution, cinematic lighting, vertical 9:16 aspect ratio"}
            ],
            "parameters": {
                "sampleCount": 1,
                "aspectRatio": "9:16",
                "outputOptions": {
                    "mimeType": "image/jpeg"
                }
            }
        }

        try:
            res = requests.post(url, json=payload, headers={"Content-Type": "application/json"})
            if not res.ok:
                print(f"  ⚠️ Scene {idx} APIエラー ({res.status_code}): {res.text}")
                continue

            data = res.json()
            predictions = data.get("predictions", [])
            if predictions and "bytesBase64Encoded" in predictions[0]:
                img_bytes = base64.b64decode(predictions[0]["bytesBase64Encoded"])
                image_path = os.path.join(output_dir, f"scene_{idx:02d}.jpg")
                with open(image_path, "wb") as f:
                    f.write(img_bytes)
                print(f"  └ 保存完了: {image_path}")
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
