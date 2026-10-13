import os
import re
import json
import requests
from pydub import AudioSegment
from discord_notifier import send_to_discord

AIVIS_URL = os.environ.get("AIVIS_URL", "http://localhost:10101")
SPEAKER_ID = int(os.environ.get("SPEAKER_ID", "888753760")) # デフォルト話者ID

def split_sentences(text):
    """台本を句読点等で1文ずつに分割"""
    lines = [s.strip() for s in re.split(r'(?<=[。！？\n])', text) if s.strip()]
    return lines

def format_srt_time(ms):
    """ミリ秒を SRT の時刻フォーマット (HH:MM:SS,mmm) に変換"""
    sec, msec = divmod(ms, 1000)
    hrs, sec = divmod(sec, 3600)
    mins, sec = divmod(sec, 60)
    return f"{hrs:02d}:{mins:02d}:{sec:02d},{msec:03d}"

def generate_voice_and_srt(script_text, output_dir="output"):
    os.makedirs(output_dir, exist_ok=True)
    sentences = split_sentences(script_text)
    
    combined_audio = AudioSegment.empty()
    srt_entries = []
    current_time_ms = 0
    
    for idx, sentence in enumerate(sentences, start=1):
        # 1. 音声クエリ作成
        query_res = requests.post(
            f"{AIVIS_URL}/audio_query",
            params={"text": sentence, "speaker": SPEAKER_ID}
        )
        query_res.raise_for_status()
        query_data = query_res.json()
        
        # 2. 音声合成
        synth_res = requests.post(
            f"{AIVIS_URL}/synthesis",
            params={"speaker": SPEAKER_ID},
            json=query_data
        )
        synth_res.raise_for_status()
        
        # 3. 単体 wav 保存
        sent_wav_path = os.path.join(output_dir, f"sent_{idx:02d}.wav")
        with open(sent_wav_path, "wb") as f:
            f.write(synth_res.content)
            
        # 4. 音声結合 & SRT計算
        audio_seg = AudioSegment.from_file(sent_wav_path)
        duration_ms = len(audio_seg)
        
        start_srt = format_srt_time(current_time_ms)
        end_srt = format_srt_time(current_time_ms + duration_ms)
        
        srt_entries.append(f"{idx}\n{start_srt} --> {end_srt}\n{sentence}\n")
        
        combined_audio += audio_seg
        current_time_ms += duration_ms
        
    # 全体音声書き出し
    full_voice_path = os.path.join(output_dir, "full_voice.wav")
    combined_audio.export(full_voice_path, format="wav")
    
    # 字幕ファイル書き出し
    with open(os.path.join(output_dir, "subtitles.srt"), "w", encoding="utf-8") as f:
        f.write("\n".join(srt_entries))
        
    duration_sec = current_time_ms / 1000
    print(f"✅ 音声・字幕生成完了 (計 {len(sentences)} 文, 全長: {duration_sec:.2f}秒)")

    # Discord 通知（全体音声ファイルを添付）
    msg = f"🎙️ **【進捗通知】音声・字幕の生成が完了しました！**\n- **文章数:** {len(sentences)} 文\n- **音声長:** {duration_sec:.2f} 秒"
    send_to_discord(msg, file_path=full_voice_path)

if __name__ == "__main__":
    # Discordからの入力またはローカルのjsonから台本を取得
    json_path = "output/trivia_result.json"
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        generate_voice_and_srt(data["script_text"])
    else:
        print(f"⚠️ {json_path} が存在しません。")
