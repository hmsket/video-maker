import json
import os
import re
import subprocess

from pydub import AudioSegment

from discord_notifier import send_to_discord

WIDTH = int(os.environ.get("VIDEO_WIDTH", "1080"))
HEIGHT = int(os.environ.get("VIDEO_HEIGHT", "1920"))
# 字幕フォントサイズ（ASS 基準解像度は 384x288 系なので、この値が縦動画にも比率で反映される）
SUB_FONT_SIZE = int(os.environ.get("SUB_FONT_SIZE") or "20")
# 字幕フォント（Actions ランナーには fonts-noto-cjk をインストール済み。独自フォントは
# ワークフロー EXTRA_FONT_URLS で /usr/share/fonts/truetype/custom にインストールして指定可）
DEFAULT_FONT_NAME = "Noto Sans CJK JP"
DEFAULT_FONT_FILE = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
SUB_FONT_NAME = os.environ.get("SUB_FONT_NAME") or DEFAULT_FONT_NAME
FONT_FILE = os.environ.get("SUB_FONT_FILE") or DEFAULT_FONT_FILE
# タイトル用フォント（未指定なら字幕と同じ物を使う）
TITLE_FONT_FILE = os.environ.get("TITLE_FONT_FILE") or FONT_FILE
# タイトル表示（イントロ）: 最初の TITLE_INTRO_SEC 秒、1つ目の素材をぼかしてタイトルを出す
TITLE_INTRO_SEC = float(os.environ.get("TITLE_INTRO_SEC") or "3")
TITLE_FONT_SIZE = int(os.environ.get("TITLE_FONT_SIZE") or "72")
VIDEO_FPS = int(os.environ.get("VIDEO_FPS", "30"))


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


def collect_clip_files(output_dir):
    """動画クリップモード用: manual-assets/clips からアップロードされたクリップを探す。

    命名は clip_XX.mp4 を推奨（XX 昇順で連結）が、mp4 があれば名前順で使う。
    draft_output.mp4（前回の出力）は除外する。
    戻り値: クリップが見つかればパスのリスト（>=1）、無ければ []
    """
    clip_dir = os.path.join(output_dir, "clips")
    src = clip_dir if os.path.isdir(clip_dir) else output_dir

    def sort_key(name):
        m = re.match(r"clip[_\-]?(\d+)", name, re.IGNORECASE)
        return (int(m.group(1)) if m else 9999, name)

    files = sorted(
        (name for name in os.listdir(src)
         if name.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))
         and "draft_output" not in name.lower()),
        key=sort_key,
    )
    return [os.path.join(src, name) for name in files]


def load_title(output_dir):
    """台本データから動画タイトルを取り出す（イントロ表示用）。"""
    path = os.path.join(output_dir, "trivia_result.json")
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f).get("title") or "").strip()
    except Exception:
        return ""


def _norm_chain(label, fps_norm, extra=""):
    """各入力を 1080x1920 / 統一fps に正規化するフィルタチェーン。"""
    # フレーム率・timebase を concat 前に揃えておく（素材ごとの fps 差を吸収）
    return (
        f"{label}scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},setsar=1{extra},{fps_norm}"
    )


def _title_intro_filter(fps_norm, title_file):
    """イントロ(segment): ぼかし + 暗め + 中央にタイトル文字。"""
    return (
        f"boxblur=luma_radius=24:luma_power=2,"
        f"eq=brightness=-0.08,"
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.35:t=fill,"
        f"drawtext=fontfile={TITLE_FONT_FILE}:textfile={title_file}"
        f":fontsize={TITLE_FONT_SIZE}:fontcolor=white"
        f":box=1:boxcolor=black@0.45:boxborderw=24"
        f":x=(w-text_w)/2:y=(h-text_h)/2,"
        f"{fps_norm},trim=0:{TITLE_INTRO_SEC:.3f},setpts=PTS-STARTPTS"
    )


def build_video(output_dir="output"):
    audio_path = os.path.join(output_dir, "full_voice.wav")
    srt_path = os.path.join(output_dir, "subtitles.srt")
    output_video_path = os.path.join(output_dir, "draft_output.mp4")

    if not os.path.exists(audio_path):
        print(f"⚠️ {audio_path} が存在しないため、動画合成をスキップします。")
        send_to_discord("⚠️ **【進捗通知】音声ファイルが見つからないため動画合成をスキップしました。**")
        return

    # ── 素材の決定: 動画クリップがあれば優先、無ければ静止画 ──
    print(f"🔤 フォント設定: 字幕 FontName={SUB_FONT_NAME} / size={SUB_FONT_SIZE}")
    print(f"🔤 フォント設定: タイトル file={TITLE_FONT_FILE} / size={TITLE_FONT_SIZE}")
    clip_files = collect_clip_files(output_dir)
    mode = "clip" if clip_files else "image"
    if clip_files:
        print(f"🎬 手動クリップモード: {len(clip_files)} 本の動画クリップを使用します")
    else:
        print("🖼️ 自動画像モード（Pollinations/アップロード画像）を使用します")

    scene_files = clip_files or collect_scene_files(output_dir)
    n = len(scene_files)

    # ── 音声長から各シーンの表示時間を算出 ──
    audio = AudioSegment.from_file(audio_path)
    total_sec = len(audio) / 1000.0
    scene_duration = total_sec / n  # 検出した素材数で均等割り

    # Subtitles フィルタ用のパス指定（コロンなどのパス特殊文字対策）
    clean_srt_path = srt_path.replace("\\", "/").replace(":", "\\:")

    # ── タイトル（イントロ）の準備 ──
    title = load_title(output_dir)
    use_intro = bool(title) and scene_duration > TITLE_INTRO_SEC + 0.5
    title_text = title or "歴史雑学ショート"
    title_file = os.path.join(output_dir, "title.txt").replace("\\", "/").replace(":", "\\:")
    with open(os.path.join(output_dir, "title.txt"), "w", encoding="utf-8") as f:
        f.write(title_text)

    # ── FFmpeg 入力コマンド構築 ──
    cmd = ["ffmpeg", "-y"]
    for asset_path in scene_files:
        if mode == "clip":
            # クリップ不足時はループで補完し、音声長で打ち切り（Veo側の音声は使わない）
            cmd += ["-stream_loop", "-1", "-t", str(scene_duration), "-i", asset_path]
        else:
            cmd += ["-loop", "1", "-t", str(scene_duration), "-i", asset_path]
    cmd += ["-i", audio_path]

    # Discord へ投稿するためビットレート制御（既定は約7.5MBに収まる目安）
    # より高画質にしたい（手動DL運用）場合は UPLOAD_TARGET_MB を上げる
    upload_target_mb = float(os.environ.get("UPLOAD_TARGET_MB") or "7.5")
    total_bps = upload_target_mb * 1024 * 1024 * 8 / max(total_sec, 1)
    video_bps = int(max(500_000, total_bps - 96_000))  # 音声 96kbps 分を差し引く

    fps_norm = f"fps={VIDEO_FPS},settb=AVTB"
    wm_crop = max(0.0, min(float(os.environ.get("WATERMARK_CROP") or "0.08"), 0.3))

    # 各素材を正規化 → [v0]..[v(n-1)]
    parts = []
    for i in range(n):
        if mode == "clip":
            chain = _norm_chain(f"[{i}:v]", fps_norm)  # クリップには可視水印が付かない想定 → 下端切り詰めなし
        else:
            extra = ""
            if wm_crop > 0:
                # 下端の水印（Pollinations クレジット）を切り、9:16 を維持したまま中央寄せ拡大
                h0 = int(HEIGHT * (1 - wm_crop))
                w1 = int(h0 * WIDTH / HEIGHT) & ~1
                h1 = int(w1 * HEIGHT / WIDTH) & ~1
                x0 = (WIDTH - w1) // 2
                extra = f",crop={w1}:{h1}:{x0}:0"
            chain = _norm_chain(f"[{i}:v]", fps_norm, extra)
        parts.append(chain + f"[v{i}];")

    labels = [f"[v{i}]" for i in range(n)]

    # ── イントロ実装: 1つ目の素材を「ぼかし+タイトル 3秒 → 通常画」に分割 ──
    if use_intro:
        parts.append("[v0]split=2[v0a][v0b];")
        parts.append(f"[v0a]{_title_intro_filter(fps_norm, title_file)}[tseg];")
        parts.append(f"[v0b]{fps_norm},trim={TITLE_INTRO_SEC:.3f},setpts=PTS-STARTPTS[rseg];")
        labels = ["[tseg]", "[rseg]"] + labels[1:]

    concat_in = "".join(labels)
    filter_complex = (
        "".join(parts)
        + f"{concat_in}concat=n={len(labels)}:v=1:a=0[v];"
        + f"[v]subtitles={clean_srt_path}:force_style='FontName={SUB_FONT_NAME},FontSize={SUB_FONT_SIZE},Alignment=2'[outv]"
    )

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-map", f"{n}:a",
        "-c:v", "libx264",
        "-b:v", str(video_bps),
        "-maxrate", str(int(video_bps * 1.4)),
        "-bufsize", str(int(video_bps * 2.5)),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "96k",
        "-shortest",
        output_video_path,
    ]

    print(f"🎬 仮組み動画を作成中 (FFmpeg, {mode}モード, {n} シーン)...")
    try:
        subprocess.run(cmd, check=True)
        print(f"🎉 仮組み動画生成完了: {output_video_path}")

        # 4. 生成された仮組み動画を Discord に投稿
        msg = (
            f"🎬 **【進捗通知】仮組み動画（draft_output.mp4）の合成が完了しました！**\n"
            f"- **素材モード:** {'手動クリップ' if mode == 'clip' else '自動画像'}\n"
            f"- **動画長:** {total_sec:.2f} 秒\n"
            f"- **シーン数:** {n}"
            + (f"\n- **イントロ:** 先頭 {TITLE_INTRO_SEC} 秒にタイトル「{title_text[:20]}」を表示" if use_intro else "")
        )
        sent = send_to_discord(msg, file_path=output_video_path)
        if not sent:
            # 容量超過（413）などで投稿できなかった場合の案内
            send_to_discord(
                "📦 **【案内】動画ファイルが大きすぎて Discord へ直接投稿できませんでした。**\n"
                "- GitHub の **Actions → 実行 → Artifacts → video-draft-assets** から zip をダウンロードしてください。\n"
                "- Discord に届くサイズにしたい場合はリポジトリ変数 `UPLOAD_TARGET_MB` を下げて再実行（例: `5`）。"
            )

    except subprocess.CalledProcessError as e:
        print(f"❌ FFmpeg エラーが発生しました: {e}")
        send_to_discord("❌ **【エラー通知】FFmpeg による動画合成に失敗しました。**")

if __name__ == "__main__":
    build_video()
