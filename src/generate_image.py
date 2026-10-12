import os
import json
from google import genai
from google.genai import types

def generate_scene_images(image_prompts, output_dir="output"):
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    os.makedirs(output_dir, exist_ok=True)
    
    for idx, prompt in enumerate(image_prompts[:3], start=1):
        print(f"🎨 Scene {idx} 画像生成中: {prompt[:40]}...")
        result = client.models.generate_images(
            model='imagen-3.0-generate-002',
            prompt=f"{prompt}, 8k resolution, cinematic lighting, vertical 9:16 aspect ratio",
            config=types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio="9:16",
                output_mime_type="image/jpeg",
            )
        )
        for generated_image in result.generated_images:
            image_path = os.path.join(output_dir, f"scene_{idx:02d}.jpg")
            with open(image_path, "wb") as f:
                f.write(generated_image.image.image_bytes)
            print(f"  └ 保存完了: {image_path}")

if __name__ == "__main__":
    with open("output/trivia_result.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    generate_scene_images(data.get("image_prompts", []))
