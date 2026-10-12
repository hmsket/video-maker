import os
import subprocess
from pydub import AudioSegment

def build_video(output_dir="output"):
    audio_path = os.path.join(output_dir, "full_voice.wav")
    audio = AudioSegment.from_file(audio_path)
    total_sec = len(audio) / 1000.0
    scene_duration = total_sec / 3.0  # 3枚の画像を均等分割割り当て

    # FFmpeg 入力コマンド構築
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-t", str(scene_duration), "-i", os.path.join(output_dir, "scene_01.jpg"),
        "-loop", "1", "-t", str(scene_duration), "-i", os.path.join(output_dir, "scene_02.jpg"),
        "-loop", "1", "-t", str(scene_duration), "-i", os.path.join(output_dir, "scene_03.jpg"),
        "-i", audio_path,
        "-filter_complex",
        "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v];"
        f"[v]subtitles={os.path.join(output_dir, 'subtitles.srt')}:force_style='FontSize=20,Alignment=2'[outv]",
        "-map", "[outv]",
        "-map", "3:a",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",
        os.path.join(output_dir, "draft_output.mp4")
    ]
    
    print("🎬 仮組み動画を作成中 (FFmpeg)...")
    subprocess.run(cmd, check=True)
    print("🎉 仮組み動画生成完了: output/draft_output.mp4")

if __name__ == "__main__":
    build_video()
