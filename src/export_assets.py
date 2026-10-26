import csv
import fnmatch
import json
import os
import zipfile

from pydub import AudioSegment

from discord_notifier import send_to_discord
from generate_voice import split_sentences

OUTPUT_DIR = "output"


def export_assets(output_dir=OUTPUT_DIR, zip_name="aviutl_assets.zip"):
    """AviUtl / CapCut 等の外部編集用に素材一式を zip にまとめる。

    同梱物:
      sent_XX.wav     句点ごとに分かれた音声（AviUtl で並べる・間を調整する用）
      full_voice.wav  全文を連結した音声
      subtitles.srt   文ごとの時間つき字幕
      script.txt      台本本文
      prompts.txt     画像プロンプト一覧（Gemini 等にコピペして画像/アニメ生成する用）
      sentences.csv   文番号・開始秒・長さ・文字数・本文（並べ替えの参考）
      title.txt       タイトル文字列
      trivia_result.json 台本データそのもの
    """
    json_path = os.path.join(output_dir, "trivia_result.json")
    if not os.path.exists(json_path):
        print(f"⚠️ {json_path} が存在しないため、素材パッケージ化をスキップします")
        return

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    script = data.get("script_text", "")
    title = data.get("title", "")
    prompts = data.get("image_prompts", [])

    # script.txt
    with open(os.path.join(output_dir, "script.txt"), "w", encoding="utf-8") as f:
        f.write(f"{title}\n\n{script}\n" if title else f"{script}\n")

    # prompts.txt（Gemini / Flow にコピペしやすいよう番号付き）
    with open(os.path.join(output_dir, "prompts.txt"), "w", encoding="utf-8") as f:
        f.write("【画像プロンプト（各シーン用）】\n")
        for i, p in enumerate(prompts, start=1):
            f.write(f"\n--- Scene {i} ---\n{p}\n")
        if not prompts:
            f.write("（プロンプトなし）\n")

    # sentences.csv（Excel/AviUtl で見ることを想定し BOM 付き UTF-8）
    sentences = split_sentences(script)
    rows = []
    current = 0.0
    for i, s in enumerate(sentences, start=1):
        wav_path = os.path.join(output_dir, f"sent_{i:02d}.wav")
        dur = 0.0
        if os.path.exists(wav_path):
            dur = len(AudioSegment.from_file(wav_path)) / 1000.0
        rows.append([i, round(current, 3), round(dur, 3), len(s), s])
        current += dur

    with open(os.path.join(output_dir, "sentences.csv"), "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["idx", "start_sec", "duration_sec", "chars", "text"])
        writer.writerows(rows)

    # title.txt（無ければ作る）
    title_path = os.path.join(output_dir, "title.txt")
    if not os.path.exists(title_path):
        with open(title_path, "w", encoding="utf-8") as f:
            f.write(title or "")

    # zip にまとめる
    include = ["sent_*.wav", "full_voice.wav", "subtitles.srt", "script.txt",
               "prompts.txt", "sentences.csv", "title.txt", "trivia_result.json"]
    zip_path = os.path.join(os.getcwd(), zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(output_dir)):
            for pat in include:
                if fnmatch.fnmatch(name, pat) and os.path.isfile(os.path.join(output_dir, name)):
                    z.write(os.path.join(output_dir, name), arcname=name)
                    break

    print(f"✅ 素材パッケージ作成完了: {zip_path}（{len(rows)} 文 / プロンプト {len(prompts)} 件）")

    # Discord に入手先を案内
    base = os.environ.get("GITHUB_SERVER_URL", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if base and repo and run_id:
        url = f"{base}/{repo}/actions/runs/{run_id}"
        send_to_discord(
            f"📦 **【進捗通知】AviUtl 編集用の素材パッケージができました！**\n"
            f"- **内容:** 句点ごとの音声 {len(rows)} 本 / 字幕 / 台本 / 画像プロンプト {len(prompts)} 件\n"
            f"- **入手:** 下のURL → Artifacts → `aviutl-assets` をダウンロード\n{url}"
        )


if __name__ == "__main__":
    export_assets()
