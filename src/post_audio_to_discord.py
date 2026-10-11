#!/usr/bin/env python3
"""生成した音声を、Discord のスレッド(trivia_result.json の thread_id)に返信として投稿する。

  python src/post_audio_to_discord.py --input output/trivia_result.json --audio output/speech.mp3

必要な環境変数: DISCORD_TOKEN(Bot トークン)
"""
import argparse
import json
import os
import sys
import time

import requests

API = "https://discord.com/api/v10"


def log(*a):
    print(*a, flush=True)


def die(msg):
    log(f"❌ {msg}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default="output/trivia_result.json")
    ap.add_argument("--audio", default="output/speech.mp3")
    ap.add_argument("--max_mb", type=float, default=9.5, help="添付サイズの上限(サーバーの上限に合わせる)")
    ap.add_argument("--credit", default=os.environ.get("VOICE_CREDIT", ""),
                    help="音声のクレジット表記(使う声の規約に従って記入)")
    a = ap.parse_args()

    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        die("DISCORD_TOKEN が未設定です。")
    try:
        with open(a.input, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        die(f"{a.input} を読めません: {e}")
    thread_id = info.get("thread_id")
    if not thread_id:
        die("trivia_result.json に thread_id がありません(Discord への投稿が完了していない可能性)。")
    if not os.path.exists(a.audio):
        die(f"音声ファイルがありません: {a.audio}")

    size_mb = os.path.getsize(a.audio) / 1024 / 1024
    if size_mb > a.max_mb:
        die(f"音声が大きすぎます({size_mb:.1f}MB > {a.max_mb}MB)。mp3 に変換するか、ビットレートを下げてください。")

    content = "🔊 **ナレーション音声**"
    if a.credit:
        content += f"\n音声: {a.credit}"

    url = f"{API}/channels/{thread_id}/messages"
    headers = {"Authorization": f"Bot {token}"}
    for attempt in range(1, 4):
        with open(a.audio, "rb") as fh:
            r = requests.post(
                url, headers=headers,
                data={"payload_json": json.dumps({"content": content})},
                files={"files[0]": (os.path.basename(a.audio), fh)},
                timeout=120,
            )
        if r.status_code == 429:
            wait = 5
            try:
                wait = min(float(r.json().get("retry_after", 5)), 30)
            except Exception:
                pass
            log(f"⚠️ レート制限。{wait:.0f}秒待って再試行します({attempt}/3)")
            time.sleep(wait)
            continue
        if r.status_code >= 400:
            die(f"Discord への投稿に失敗しました: HTTP {r.status_code} {r.text[:300]}")
        log(f"🎉 音声を投稿しました: thread {thread_id} ({size_mb:.1f}MB)")
        return 0
    die("レート制限が解消されず、投稿できませんでした。")


if __name__ == "__main__":
    sys.exit(main())
