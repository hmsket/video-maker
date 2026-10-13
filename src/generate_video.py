import os
import subprocess
from pydub import AudioSegment
from discord_notifier import send_to_discord

def create_placeholder_image(output_path, width=1080, height=1920):
    """画像が存在しない場合の黒背景プレースホルダー画像作成 (FFmpeg)"""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=black:s={width}x{height}",
        "-vframes", "1",
        output_path
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def build_video(output_dir="output"):
    audio_path = os.path.join(output_dir, "full_voice.wav")
    srt_path = os.path.join(output_dir, "subtitles.srt")
    output_video_path = os.path.join(output_dir, "draft_output.mp4")

    if not os.path.exists(audio_path):
        print(f"⚠️ {audio_path} が存在しないため、動画合成をスキップします。")
        send_to_discord("⚠️ **【進捗通知】音声ファイルが見つからないため動画合成をスキップしました。**")
        return

    # 1. 各シーン画像の存在チェック＆フォールバック生成
    scene_files = []
    for i in range(1, 4):
        img_path = os.path.join(output_dir, f"scene_{i:02d}.jpg")
        if not os.path.exists(img_path):
            print(f"⚠️ {img_path} が無いため、プレースホルダー黒背景画像を生成します。")
            create_placeholder_image(img_path)
        scene_files.append(img_path)

    # 2. 音声長から各シーンの表示時間を算出
    audio = AudioSegment.from_file(audio_path)
    total_sec = len(audio) / 1000.0
    scene_duration = total_sec / 3.0  # 3枚の画像を均等分割割り当て

    # Subtitles フィルタ用のパス指定（コロンなどのパス特殊文字対策）
    clean_srt_path = srt_path.replace("\\", "/").replace(":", "\\:")

    # 3. FFmpeg 入力コマンド構築
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-t", str(scene_duration), "-i", scene_files[0],
        "-loop", "1", "-t", str(scene_duration), "-i", scene_files[1],
        "-loop", "1", "-t", str(scene_duration), "-i", scene_files[2],
        "-i", audio_path,
        "-filter_complex",
        f"[0:v][1:v][2:v]concat=n=3:v=1:a=0[v];"
        f"[v]subtitles={clean_srt_path}:force_style='FontSize=20,Alignment=2'[outv]",
        "-map", "[outv]",
        "-map", "3:a",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",
        output_video_path
    ]
    
    print("🎬 仮組み動画を作成中 (FFmpeg)...")
    try:
        subprocess.run(cmd, check=True)
        print(f"🎉 仮組み動画生成完了: {output_video_path}")

        # 4. 生成された仮組み動画を Discord に投稿
        msg = f"🎬 **【進捗通知】仮組み動画（draft_output.mp4）の合成が完了しました！**\n- **動画長:** {total_sec:.2f} 秒"
        send_to_discord(msg, file_path=output_video_path)

    except subprocess.CalledProcessError as e:
        print(f"❌ FFmpeg エラーが発生しました: {e}")
        send_to_discord("❌ **【エラー通知】FFmpeg による動画合成に失敗しました。**")

if __name__ == "__main__":
    build_video()
