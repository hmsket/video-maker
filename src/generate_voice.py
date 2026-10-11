#!/usr/bin/env python3
"""台本JSON → 文ごとに音声合成 → speech.wav(連結) + subtitles.json + subtitles.srt

AivisSpeech Engine(VOICEVOX 互換の HTTP API)を使います。
  GET  /version, /speakers
  POST /audio_query?text=...&speaker=<スタイルID>
  POST /synthesis?speaker=<スタイルID>      (body: audio_query の結果)

使い方:
  python src/generate_voice.py --input output/trivia_result.json --speaker 123456789 --output_dir output
  python src/generate_voice.py --check                      # エンジンへの接続と話者一覧だけ確認

※ 文ごとに合成するので、字幕の時刻が正確に出せます。
※ 使う声(音声モデル)の利用規約・クレジット表記は、公開前に必ず確認してください。
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
import wave

import requests

DEFAULT_URL = os.environ.get("AIVIS_URL", "http://127.0.0.1:10101")


def log(*a):
    print(*a, flush=True)


def die(msg, code=1):
    log(f"❌ {msg}")
    sys.exit(code)


# ------------------------------------------------------------------ エンジンとの通信
def get_speaker_ids(session, base):
    """{スタイルID: '話者名 / スタイル名'}。取れなければ None。"""
    try:
        r = session.get(f"{base}/speakers", timeout=30)
        r.raise_for_status()
        return {st["id"]: f"{sp['name']} / {st['name']}"
                for sp in r.json() for st in sp.get("styles", [])}
    except Exception as e:
        log(f"⚠️ /speakers を取得できませんでした: {e}")
        return None


def preflight(session, base, speaker):
    try:
        r = session.get(f"{base}/version", timeout=15)
        r.raise_for_status()
        log(f"🔊 エンジン接続OK: {base}  version={r.text.strip()}")
    except Exception as e:
        die(f"エンジンに接続できません({base}): {e}")
    ids = get_speaker_ids(session, base)
    if ids is None:
        return
    log(f"   話者(スタイル)数: {len(ids)}")
    if speaker is not None and speaker not in ids:
        sample = "\n".join(f"     {i}\t{n}" for i, n in list(ids.items())[:15])
        die(f"話者ID {speaker} は、このエンジンにありません。使えるIDの例:\n{sample}", code=2)
    if speaker is not None:
        log(f"   使用する声: {ids[speaker]} (ID {speaker})")


def synth(session, base, text, speaker, opts, retries=3):
    """1文を合成して、WAV のバイト列を返す。"""
    last = None
    for i in range(retries):
        try:
            q = session.post(f"{base}/audio_query", params={"text": text, "speaker": speaker}, timeout=60)
            q.raise_for_status()
            query = q.json()
            for k, v in opts.items():
                if v is not None and k in query:      # エンジンにない項目は、設定しない
                    query[k] = v
            r = session.post(f"{base}/synthesis", params={"speaker": speaker}, json=query, timeout=180)
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            last = e
            log(f"   ⚠️ 合成エラー({type(e).__name__}): 再試行 {i + 1}/{retries}")
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"音声合成に失敗しました: {last}")


# ------------------------------------------------------------------ WAV の扱い
def ffmpeg_convert(wav_bytes, rate=None, channels=None):
    """16bit PCM の WAV に変換する(float形式や、サンプルレート違いの対策)。"""
    cmd = ["ffmpeg", "-v", "error", "-i", "pipe:0", "-acodec", "pcm_s16le"]
    if rate:
        cmd += ["-ar", str(rate)]
    if channels:
        cmd += ["-ac", str(channels)]
    cmd += ["-f", "wav", "pipe:1"]
    r = subprocess.run(cmd, input=wav_bytes, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg での変換に失敗: {r.stderr.decode(errors='ignore')[-300:]}")
    return r.stdout


def read_wav(wav_bytes):
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        return (w.getnchannels(), w.getsampwidth(), w.getframerate()), w.readframes(w.getnframes())


def load_pcm(wav_bytes, want=None):
    """(形式, フレーム) を返す。16bit PCM でない/形式が want と違う場合は、ffmpeg で合わせる。"""
    try:
        fmt, frames = read_wav(wav_bytes)
        if fmt[1] == 2 and (want is None or fmt == want):
            return fmt, frames
    except wave.Error:
        pass                                           # float WAV など
    rate = want[2] if want else None
    ch = want[0] if want else None
    return read_wav(ffmpeg_convert(wav_bytes, rate, ch))


def silence(fmt, sec):
    ch, width, rate = fmt
    return b"\x00" * (ch * width * int(rate * sec))


# ------------------------------------------------------------------ 字幕
def split_cues(text, max_chars):
    """長い文を、読点などで、字幕1枚に収まる長さに区切る。"""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]
    parts = re.findall(r"[^、。！？!?]+[、。！？!?]?", text) or [text]
    chunks, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) > max_chars:
            chunks.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        chunks.append(cur)
    out = []
    for c in chunks:
        while len(c) > max_chars * 1.5:
            out.append(c[:max_chars])
            c = c[max_chars:]
        out.append(c)
    return [c for c in out if c]


def clean_cue(text):
    return re.sub(r"[。]+$", "", text.strip())


def wrap_lines(text, line_chars):
    """縦動画で読みやすいように、字幕を改行で区切る(句読点の直後を優先)。"""
    if len(text) <= line_chars:
        return text
    lines, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= line_chars and ch not in "、，,":
            lines.append(cur)
            cur = ""
        elif len(cur) >= line_chars - 2 and ch in "、，,":
            lines.append(cur)
            cur = ""
    if cur:
        if lines and len(cur) <= 2:                  # 1〜2文字だけ残るのを防ぐ
            lines[-1] += cur
        else:
            lines.append(cur)
    return "\n".join(lines)


def srt_time(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(cues, line_chars):
    out = []
    for n, c in enumerate(cues, 1):
        out.append(f"{n}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n{wrap_lines(c['text'], line_chars)}\n")
    return "\n".join(out)


# ------------------------------------------------------------------ main
def load_sentences(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    sents = data.get("sentences")
    if not sents:                                        # 古い形式: script_text を文に分ける
        text = data.get("script_text") or ""
        sents = [{"text": t} for t in re.findall(r"[^。！？]+[。！？]?", text) if t.strip()]
    sents = [s for s in sents if (s.get("narration") or s.get("text") or "").strip()]
    if not sents:
        die("台本に、読み上げる文がありません。")
    return data, sents


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default="output/trivia_result.json")
    ap.add_argument("--speaker", type=int, help="話者(スタイル)ID。/speakers で確認")
    ap.add_argument("--output_dir", default="output")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--check", action="store_true", help="接続確認と話者一覧の表示だけ行う")
    ap.add_argument("--speed", type=float, default=1.1, help="話速(speedScale)")
    ap.add_argument("--pitch", type=float, default=None)
    ap.add_argument("--intonation", type=float, default=None)
    ap.add_argument("--volume", type=float, default=None)
    ap.add_argument("--pause", type=float, default=0.3, help="文と文の間の無音(秒)")
    ap.add_argument("--lead", type=float, default=0.2, help="冒頭の無音(秒)")
    ap.add_argument("--cue_chars", type=int, default=24, help="字幕1枚の最大文字数")
    ap.add_argument("--line_chars", type=int, default=12, help="字幕1行の文字数(SRTの改行位置)")
    a = ap.parse_args()

    base = a.url.rstrip("/")
    session = requests.Session()

    if a.check:
        preflight(session, base, None)
        ids = get_speaker_ids(session, base) or {}
        for i, n in ids.items():
            log(f"{i}\t{n}")
        return 0
    if a.speaker is None:
        die("--speaker を指定してください(--check で一覧を確認できます)。", code=2)

    data, sents = load_sentences(a.input)
    preflight(session, base, a.speaker)

    opts = {"speedScale": a.speed, "pitchScale": a.pitch,
            "intonationScale": a.intonation, "volumeScale": a.volume}

    fmt, chunks, t = None, [], 0.0
    result = []
    for i, s in enumerate(sents):
        text = (s.get("narration") or s.get("text")).strip()
        log(f"[{i + 1}/{len(sents)}] {text[:30]}")
        raw = synth(session, base, text, a.speaker, opts)
        fmt_i, frames = load_pcm(raw, fmt)
        if fmt is None:
            fmt = fmt_i
            lead = silence(fmt, a.lead)
            chunks.append(lead)
            t += a.lead
        n_frames = len(frames) // (fmt[0] * fmt[1])
        dur = n_frames / fmt[2]
        start, end = t, t + dur
        chunks.append(frames)
        t = end
        if i < len(sents) - 1:
            chunks.append(silence(fmt, a.pause))
            t += a.pause
        cues = []
        parts = [clean_cue(c) for c in split_cues(s.get("text") or text, a.cue_chars)]
        parts = [p for p in parts if p]
        total_len = sum(len(p) for p in parts) or 1
        cur = start
        for p in parts:
            d = dur * len(p) / total_len
            cues.append({"text": p, "start": round(cur, 3), "end": round(cur + d, 3)})
            cur += d
        result.append({"index": i, "part": s.get("part"), "type": s.get("type"),
                       "text": s.get("text") or text, "start": round(start, 3), "end": round(end, 3),
                       "cues": cues})

    os.makedirs(a.output_dir, exist_ok=True)
    wav_path = os.path.join(a.output_dir, "speech.wav")
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(fmt[0])
        w.setsampwidth(fmt[1])
        w.setframerate(fmt[2])
        w.writeframes(b"".join(chunks))
    flat = [c for r in result for c in r["cues"]]
    with open(os.path.join(a.output_dir, "subtitles.json"), "w", encoding="utf-8") as f:
        json.dump({"duration": round(t, 3), "sample_rate": fmt[2], "speaker": a.speaker,
                   "sentences": result, "cues": flat}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(a.output_dir, "subtitles.srt"), "w", encoding="utf-8") as f:
        f.write(build_srt(flat, a.line_chars))

    log(f"✅ 完了: {wav_path}  {t:.1f}秒 / 文{len(result)} / 字幕{len(flat)}枚")
    if t > 180:
        log("⚠️ 3分を超えています。Shorts は3分までです。")
    elif t > 60:
        log("ℹ️ 60秒を超えています。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
