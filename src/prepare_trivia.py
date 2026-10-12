import os
import json
import re
import requests

def prepare_trivia():
    os.makedirs("output", exist_ok=True)
    json_path = "output/trivia_result.json"

    if os.path.exists(json_path):
        print(f"ℹ️ {json_path} は既に存在します。")
        return

    thread_id = os.environ.get("THREAD_ID")
    token = os.environ.get("DISCORD_TOKEN")
    script_text = ""

    # Discord Thread ID が渡されている場合、スレッドメッセージから確定台本を取得
    if thread_id and token:
        print(f"🔍 Discord Thread ({thread_id}) から確定台本を取得中...")
        url = f"https://discord.com/api/v10/channels/{thread_id}/messages?limit=15"
        try:
            res = requests.get(url, headers={"Authorization": f"Bot {token}"})
            if res.ok:
                messages = res.json()
                for m in messages:
                    content = m.get("content", "")
                    if "```" in content:
                        match = re.search(r"```(?:\w+)?\n?([\s\S]*?)```", content)
                        if match:
                            script_text = match.group(1).strip()
                            print("✅ Discord から台本テキストを抽出しました。")
                            break
            else:
                print(f"⚠️ Discord API エラー: {res.status_code}")
        except Exception as e:
            print(f"⚠️ Discord 取得例外: {e}")

    # フォールバック（手動実行時など台本がない場合）
    if not script_text:
        print("⚠️ 台本が見つからなかったため、テスト用台本を使用します。")
        script_text = "これはテスト用の歴史雑学台本です。正常に音声と画像が生成されるか確認しています。"

    data = {
        "script_text": script_text,
        "image_prompts": [
            "A cinematic historical reenactment scene, 8k, vertical 9:16",
            "An ancient castle with atmospheric lighting, 8k, vertical 9:16",
            "A dramatic historical moment, 8k, vertical 9:16"
        ],
        "video_prompts": ["Slow zoom in", "Pan left", "Static shot"]
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✅ {json_path} を作成・準備しました。")

if __name__ == "__main__":
    prepare_trivia()
