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


# ── 字幕の自然な改行（日本語用） ─────────────────────────────────────
# 1行あたりの目安文字数。384x288 の ASS 基準に対する字幕フォントサイズとの兼ね合いで調整
SUB_LINE_MAX_CHARS = int(os.environ.get("SUB_LINE_MAX_CHARS") or "16")

# この文字の「直後」で改行すると自然（句読点）
_BREAK_AFTER = set("、。！？…")
# 助詞（1文字）の直後も改行候補（厳密ではないが日本語らしさの近似）
_SOFT_BREAK_AFTER = set("はがをにてでとも")
# 2文字の助詞・修飾語の直後
_TWO_CHAR_BREAKS = {"から", "まで", "より", "けど", "って", "でも", "には", "での", "そして"}
# 行頭に来てはいけない文字（禁則処理）
_KINSOKU_HEADS = set("、。！？…」』）］}〜：;")

def wrap_japanese(text, max_chars=SUB_LINE_MAX_CHARS):
    """1文を日本語として自然な位置で改行して複数行にする。

    優先順位: 句読点の直後 > 助詞の直後 > 強制折返し(禁則処理あり)
    """
    text = text.strip()
    if len(text) <= max_chars:
        return text

    lines = []
    remaining = text
    half = max(1, max_chars // 2)
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = 0
        # 1) 句読点の直後（最も右側にあるものを優先）
        for i in range(max_chars, half, -1):
            if window[i - 1] in _BREAK_AFTER:
                cut = i
                break
        # 2) 助詞の直後
        if not cut:
            for i in range(max_chars, half, -1):
                sfx2 = remaining[i - 2:i]
                if sfx2 in _TWO_CHAR_BREAKS or remaining[i - 1] in _SOFT_BREAK_AFTER:
                    cut = i
                    break
        # 3) 強制折返し（次の行頭が句読点/閉じ括弧なら1文字前へ）
        if not cut:
            cut = max_chars
            while cut > 1 and remaining[cut] in _KINSOKU_HEADS:
                cut -= 1
        lines.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        lines.append(remaining)
    return "\n".join(lines)

def format_srt_time(ms):
    """ミリ秒を SRT の時刻フォーマット (HH:MM:SS,mmm) に変換"""
    sec, msec = divmod(ms, 1000)
    hrs, sec = divmod(sec, 3600)
    mins, sec = divmod(sec, 60)
    return f"{hrs:02d}:{mins:02d}:{sec:02d},{msec:03d}"

def scenes_mode(data):
    """trivia_result.json が 30秒シーン構成 (scenes) なら True。"""
    return isinstance(data.get("scenes"), list) and len(data.get("scenes", [])) >= 3


def generate_voice_and_srt(script_text, output_dir="output", units=None, skip_first_srt=False):
    """音声・SRT を生成する。

    units が指定された場合（30秒シーン構成モード）は
    (ナレーション, 画面字幕テキスト, 台本上の秒数) のリストで1シーン1音声にする。
    skip_first_srt=True で最初の要素の字幕を出力しない（タイトルカード被り防止）。
    """
    os.makedirs(output_dir, exist_ok=True)
    if units is None:
        sentences = split_sentences(script_text)
        units = [(s, s, 0.0) for s in sentences]
    items = list(units)

    combined_audio = AudioSegment.empty()
    srt_entries = []
    current_time_ms = 0

    for idx, (sentence, caption, _scripted) in enumerate(items, start=1):
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

        if not (skip_first_srt and idx == 1):
            srt_entries.append(f"{idx}\n{start_srt} --> {end_srt}\n{wrap_japanese(caption)}\n")
        
        combined_audio += audio_seg
        current_time_ms += duration_ms
        
    # 全体音声書き出し
    full_voice_path = os.path.join(output_dir, "full_voice.wav")
    combined_audio.export(full_voice_path, format="wav")
    
    # 字幕ファイル書き出し
    with open(os.path.join(output_dir, "subtitles.srt"), "w", encoding="utf-8") as f:
        f.write("\n".join(srt_entries))
        
    duration_sec = current_time_ms / 1000
    label = "シーン" if units else "文"
    n_items = len(items)
    print(f"✅ 音声・字幕生成完了 (計 {n_items} {label}, 全長: {duration_sec:.2f}秒)")

    # Discord 通知（全体音声ファイルを添付）
    msg = f"🎙️ **【進捗通知】音声・字幕の生成が完了しました！**\n- **要素数:** {n_items} {label}\n- **音声長:** {duration_sec:.2f} 秒"
    send_to_discord(msg, file_path=full_voice_path)

    return duration_sec, n_items

if __name__ == "__main__":
    # Discordからの入力またはローカルのjsonから台本を取得
    json_path = "output/trivia_result.json"
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if scenes_mode(data):
            units = [
                (s.get("narration", ""), s.get("screen_text", ""), float(s.get("duration_sec") or 0))
                for s in data["scenes"]
            ]
            # Scene 0 はタイトルカードが表示されるので字幕は省略
            generate_voice_and_srt(data.get("script_text", ""), units=units, skip_first_srt=True)
        else:
            generate_voice_and_srt(data["script_text"])
    else:
        print(f"⚠️ {json_path} が存在しません。")
