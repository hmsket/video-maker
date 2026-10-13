# src/discord_notifier.py
import os
import requests

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
THREAD_ID = os.getenv("THREAD_ID")

def send_to_discord(message: str, file_path: str = None) -> bool:
    """
    Discord の指定スレッドへメッセージやファイルを送信する
    """
    if not DISCORD_TOKEN or not THREAD_ID:
        print("[WARN] DISCORD_TOKEN または THREAD_ID が未設定のため、Discord 投稿をスキップします。")
        return False

    url = f"https://discord.com/api/v10/channels/{THREAD_ID}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}"
    }

    try:
        if file_path and os.path.exists(file_path):
            with open(file_path, "rb") as f:
                files = {
                    "file": (os.path.basename(file_path), f)
                }
                data = {
                    "content": message
                }
                res = requests.post(url, headers=headers, data=data, files=files, timeout=30)
        else:
            payload = {
                "content": message
            }
            res = requests.post(url, headers=headers, json=payload, timeout=30)

        if res.status_code in [200, 201]:
            print(f"[SUCCESS] Discord 通知送信完了: {message[:30]}...")
            return True
        else:
            print(f"[ERROR] Discord 通知失敗 ({res.status_code}): {res.text}")
            return False

    except Exception as e:
        print(f"[ERROR] Discord 通知中に例外が発生: {e}")
        return False
