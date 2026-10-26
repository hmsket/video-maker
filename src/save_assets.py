"""素材実行（assets_only）の成果物を assets/<thread_id>/ に保存してリポジトリにコミットする準備をする。

コミット / push 自体はワークフローの bash 側で行う。
"""
import os
import shutil

OUTPUT_DIR = "output"


def save_assets(output_dir=OUTPUT_DIR):
    thread_id = os.environ.get("THREAD_ID", "").strip()
    if not thread_id:
        print("⚠️ THREAD_ID を取得できないため、素材の保存をスキップします")
        return
    if not os.path.isdir(output_dir):
        print(f"⚠️ {output_dir} が存在しないためスキップします")
        return

    dest = os.path.join("assets", thread_id)
    os.makedirs(dest, exist_ok=True)

    keep_exact = [
        "trivia_result.json",
        "full_voice.wav",
        "subtitles.srt",
        "title.txt",
        "script.txt",
        "prompts.txt",
        "sentences.csv",
    ]
    copied = []
    for name in keep_exact:
        src = os.path.join(output_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest, name))
            copied.append(name)
    for name in sorted(os.listdir(output_dir)):
        if name.startswith("sent_") and name.endswith(".wav"):
            shutil.copy2(os.path.join(output_dir, name), os.path.join(dest, name))
            copied.append(name)

    print(f"✅ {dest} に {len(copied)} ファイルを保存しました（次の git push で確定します）")


if __name__ == "__main__":
    save_assets()
