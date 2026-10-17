import os
import re
import subprocess

from pydub import AudioSegment

from discord_notifier import send_to_discord

WIDTH = int(os.environ.get("VIDEO_WIDTH", "1080"))
HEIGHT = int(os.environ.get("VIDEO_HEIGHT", "1920"))
# 字幕フォントサイズ（ASS の基準解像度は 384x288 系なので、この値が縦動画にも比率で反映される）
SUB_FONT_SIZE = int(os.environ.get("SUB_FONT_SIZE", "20"))


def create_placeholder_image(output_path, width=WIDTH, height=HEIGHT):
    """画像が存在しない場合の黒背景プレースホルダー画像作成 (FFmpeg)"""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=black:s={width}x{height}",
        "-vframes", "1",
        output_path
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def collect_scene_files(output_dir):
    """scene_XX.jpg を番号順 (1 〜 最大番号) に並べる。

    画像生成に失敗して番号が欠けた場合は黒背景プレースホルダーで補完する。
    1枚も無い場合は黒背景を1枚だけ作る。
    """
    indices = sorted(
        int(m.group(1))
        for name in os.listdir(output_dir)
        if (m := re.match(r"scene_(\d+)\.jpg$", name))
    )

    if not indices:
        print("⚠️ scene_XX.jpg が1枚も無いため、黒背景を1枚だけ生成します。")
        path = os.path.join(output_dir, "scene_01.jpg")
        create_placeholder_image(path)
        return [path]

    files = []
    for i in range(1, max(indices) + 1):
        path = os.path.join(output_dir, f"scene_{i:02d}.jpg")
        if not os.path.exists(path):
            print(f"⚠️ {path} が無いため、プレースホルダー黒背景画像を生成します。")
            create_placeholder_image(path)
        files.append(path)
    return files


def build_video(output_dir="output"):
    audio_path = os.path.join(output_dir, "full_voice.wav")
    srt_path = os.path.join(output_dir, "subtitles.srt")
    output_video_path = os.path.join(output_dir, "draft_output.mp4")

    if not os.path.exists(audio_path):
        print(f"⚠️ {audio_path} が存在しないため、動画合成をスキップします。")
        send_to_discord("⚠️ **【進捗通知】音声ファイルが見つからないため動画合成をスキップしました。**")
        return

    # 1. 各シーン画像を収集（欠番はプレースホルダー補完、枚数は可変）
    scene_files = collect_scene_files(output_dir)
    n = len(scene_files)

    # 2. 音声長から各シーンの表示時間を算出
    audio = AudioSegment.from_file(audio_path)
    total_sec = len(audio) / 1000.0
    scene_duration = total_sec / n  # 検出した画像数で均等割り

    # Subtitles フィルタ用のパス指定（コロンなどのパス特殊文字対策）
    clean_srt_path = srt_path.replace("\\", "/").replace(":", "\\:")

    # 3. FFmpeg 入力コマンド構築
    cmd = ["ffmpeg", "-y"]
    for img_path in scene_files:
        cmd += ["-loop", "1", "-t", str(scene_duration), "-i", img_path]
    cmd += ["-i", audio_path]

    concat_in = "".join(f"[{i}:v]" for i in range(n))
    filter_complex = (
        f"{concat_in}concat=n={n}:v=1:a=0[v];"
        f"[v]subtitles={clean_srt_path}:force_style='FontSize={SUB_FONT_SIZE},Alignment=2'[outv]"
    )
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-map", f"{n}:a",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",
        output_video_path,
    ]

    print(f"🎬 仮組み動画を作成中 (FFmpeg, {n} シーン)...")
    try:
        subprocess.run(cmd, check=True)
        print(f"🎉 仮組み動画生成完了: {output_video_path}")

        # 4. 生成された仮組み動画を Discord に投稿
        msg = f"🎬 **【進捗通知】仮組み動画（draft_output.mp4）の合成が完了しました！**\n- **動画長:** {total_sec:.2f} 秒\n- **シーン数:** {n}"
        send_to_discord(msg, file_path=output_video_path)

    except subprocess.CalledProcessError as e:
        print(f"❌ FFmpeg エラーが発生しました: {e}")
        send_to_discord("❌ **【エラー通知】FFmpeg による動画合成に失敗しました。**")

if __name__ == "__main__":
    build_video()
