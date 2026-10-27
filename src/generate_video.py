import json
import os
import re
import subprocess

from pydub import AudioSegment

from discord_notifier import send_to_discord
from generate_voice import wrap_japanese

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
# ── CapCut 風の見た目の調整 ──────────────────────────────────────────
TITLE_LINE_MAX_CHARS = int(os.environ.get("TITLE_LINE_MAX_CHARS") or "12")  # タイトルの折返し文字数
TITLE_BG_DARK = float(os.environ.get("TITLE_BG_DARK") or "0.5")      # イントロの暗幕の強さ (0-1)
SUB_BOX_OPACITY = float(os.environ.get("SUB_BOX_OPACITY") or "0.6")  # 字幕の黒箱の濃さ (0-1, 1=不透明黒)
SUB_BOX_PAD = int(os.environ.get("SUB_BOX_PAD") or "10")             # 黒箱の内側の余白（ASSのOutline値）
SUB_MARGIN_V = int(os.environ.get("SUB_MARGIN_V") or "62")           # 字幕の下位置（ASS基準座標）
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
    """scene_XX.* を番号順 (1 〜 最大番号) に並べる。

    画像生成に失敗して番号が欠けた場合は黒背景プレースホルダーで補完する。
    1枚も無い場合は黒背景を1枚だけ作る。
    """
    indices = sorted(
        int(m.group(1))
        for name in os.listdir(output_dir)
        if (m := re.match(r"scene_(\d+)\.(jpg|jpeg|png|webp)$", name, re.I))
    )

    if not indices:
        print("⚠️ scene_XX.jpg が1枚も無いため、黒背景を1枚だけ生成します。")
        path = os.path.join(output_dir, "scene_01.jpg")
        create_placeholder_image(path)
        return [path]

    files = []
    for i in range(1, max(indices) + 1):
        found = next(
            (name for name in os.listdir(output_dir)
             if (m := re.match(rf"scene_{i:02d}\.(jpg|jpeg|png|webp)$", name, re.I))),
            None,
        )
        path = os.path.join(output_dir, found) if found else os.path.join(output_dir, f"scene_{i:02d}.jpg")
        if not os.path.exists(path):
            print(f"⚠️ scene_{i:02d} が無いため、プレースホルダー黒背景画像を生成します。")
            create_placeholder_image(path)
        files.append(path)
    return files


def collect_assets(output_dir):
    """番号つき素材を集めて1本のリストにまとめる（混合モード）。

    - clip_XX.mp4（output/clips/ または output/）→ 動画クリップ
    - scene_XX.jpg/png（output/）→ 静止画
    - 同じ番号に両方があればクリップを優先
    - 番号が空洞の場合は黒背景画像で補完
    戻り値: [(kind, path)]  kind は "clip" / "image"
    """
    clip_dir = os.path.join(output_dir, "clips")
    src = clip_dir if os.path.isdir(clip_dir) else output_dir

    clips = {}
    for name in sorted(os.listdir(src)):
        if name.lower().endswith((".mp4", ".mov", ".webm", ".mkv")) and "draft_output" not in name.lower():
            if (m := re.match(r"clip[_\-]?(\d+)", name, re.I)):
                clips[int(m.group(1))] = os.path.join(src, name)

    scenes = {}
    for name in os.listdir(output_dir):
        if (m := re.match(r"scene_(\d+)\.(jpg|jpeg|png|webp)$", name, re.I)):
            scenes[int(m.group(1))] = os.path.join(output_dir, name)

    nums = sorted(set(clips) | set(scenes))
    if not nums:
        print("⚠️ 素材が1つも無いため、黒背景を1枚だけ生成します。")
        path = os.path.join(output_dir, "scene_01.jpg")
        create_placeholder_image(path)
        return [("image", path)]

    assets = []
    max_n = max(nums)
    for i in range(1, max_n + 1):
        if i in clips:
            assets.append(("clip", clips[i]))
        elif i in scenes:
            assets.append(("image", scenes[i]))
        else:
            print(f"⚠️ 番号 {i} の素材が無いため、黒背景画像で補完します（ファイル名の番号をご確認ください）")
            path = os.path.join(output_dir, f"scene_{i:02d}.jpg")
            create_placeholder_image(path)
            assets.append(("image", path))
    return assets


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
    """イントロsegment: ぼかし + 暗め + 中央にタイトル文字（CapCut風: 白文字+影、箱なし）。"""
    return (
        f"boxblur=luma_radius=24:luma_power=2,"
        f"eq=brightness=-0.08,"
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@{TITLE_BG_DARK}:t=fill,"
        f"drawtext=fontfile={TITLE_FONT_FILE}:textfile={title_file}"
        f":fontsize={TITLE_FONT_SIZE}:fontcolor=white"
        f":line_spacing={int(TITLE_FONT_SIZE * 0.35)}"
        f":shadowcolor=black@0.7:shadowx=2:shadowy=2"
        f":x=(w-text_w)/2:y=(h-text_h)/2,"
        f"{fps_norm},trim=0:{TITLE_INTRO_SEC:.3f},setpts=PTS-STARTPTS"
    )


def _subtitle_style():
    """ASS force_style: 半透明黒箱＋白文字・下中央寄せ（CapCut風）。"""
    # ASS の alpha バイト: 00=不透明 / FF=完全透明
    alpha = int(round(255 * (1 - SUB_BOX_OPACITY)))
    return (
        f"FontName={SUB_FONT_NAME},FontSize={SUB_FONT_SIZE},"
        f"PrimaryColour=&HFFFFFF&,"
        f"BorderStyle=4,BackColour=&H{alpha:02X}000000&,"
        f"Outline={SUB_BOX_PAD},Shadow=0,"
        f"Alignment=2,MarginV={SUB_MARGIN_V}"
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
    if clip_files and any(
        not re.match(r"clip[_\-]?\d", os.path.basename(p), re.I) for p in clip_files
    ):
        print("ℹ️ 番号の無いクリップは投稿順に採番されます（古い添付ほど先頭）")
    assets = collect_assets(output_dir)
    kinds = [k for k, _ in assets]
    scene_files = [p for _, p in assets]
    n = len(scene_files)
    mode = "clip" if "clip" in kinds else "image"
    n_clip = sum(1 for k in kinds if k == "clip")
    n_image = len(kinds) - n_clip
    print(f"🎬 混合素材モード: 動画クリップ {n_clip} 本 / 静止画 {n_image} 枚（この順で連結）")
    for i, (k, p) in enumerate(assets, start=1):
        print(f"   Scene {i}: [{k}] {os.path.basename(p)}")

    # ── 音声長から各シーンの表示時間を算出 ──
    audio = AudioSegment.from_file(audio_path)
    total_sec = len(audio) / 1000.0
    scene_duration = total_sec / n  # 検出した素材数で均等割り（旧形式のフォールバック）

    # 30秒シーン構成: 台本の scenes[].duration_sec を優先（音声の実長に比率スケールして同期保証）
    scenes_list = []
    try:
        with open(os.path.join(output_dir, "trivia_result.json"), encoding="utf-8") as f:
            scenes_list = json.load(f).get("scenes") or []
    except Exception:
        scenes_list = []
    scripted = []
    for s in scenes_list:
        try:
            scripted.append(float(s.get("duration_sec") or 0))
        except Exception:
            scripted.append(0.0)
    if scripted and sum(1 for x in scripted if x > 0) == n:
        scale = total_sec / sum(scripted)
        durs = [d * scale if d > 0 else scene_duration for d in scripted]
        print(f"⏱️ シーン構成モード: 台本の尺を優先します（合計 {sum(durs):.2f} 秒 = 音声長）")
    else:
        durs = [scene_duration] * n
        if scenes_list and len(scenes_list) != n:
            print(f"⚠️ scenes の数({len(scenes_list)})と素材数({n})が異なるため、均等割りで続行します")
    is_scene_mode = bool(scenes_list)
    scene_duration = durs[0]

    # Subtitles フィルタ用のパス指定（コロンなどのパス特殊文字対策）
    clean_srt_path = srt_path.replace("\\", "/").replace(":", "\\:")

    # ── タイトル（イントロ）の準備 ──
    title = load_title(output_dir)
    # シーン構成モードでは Scene 0 自体がタイトルシーンなので、ぼかしイントロは使わない
    use_intro = bool(title) and not is_scene_mode and scene_duration > TITLE_INTRO_SEC + 0.5
    title_text = title or "歴史雑学ショート"
    title_file = os.path.join(output_dir, "title.txt").replace("\\", "/").replace(":", "\\:")
    # タイトルは日本語として自然な位置で折り返してから使う（CapCut風の2行センタリング）
    with open(os.path.join(output_dir, "title.txt"), "w", encoding="utf-8") as f:
        f.write(wrap_japanese(title_text, TITLE_LINE_MAX_CHARS))

    # ── FFmpeg 入力コマンド構築 ──
    cmd = ["ffmpeg", "-y"]
    for i, asset_path in enumerate(scene_files):
        if kinds[i] == "clip":
            # クリップ不足時はループで補完し、シーン尺で打ち切り（Veo側の音声は使わない）
            cmd += ["-stream_loop", "-1", "-t", f"{durs[i]:.3f}", "-i", asset_path]
        else:
            cmd += ["-loop", "1", "-t", f"{durs[i]:.3f}", "-i", asset_path]
    cmd += ["-i", audio_path]

    # Discord へ投稿するためビットレート制御（既定は約7.5MBに収まる目安）
    # より高画質にしたい（手動DL運用）場合は UPLOAD_TARGET_MB を上げる
    upload_target_mb = float(os.environ.get("UPLOAD_TARGET_MB") or "7.5")
    total_bps = upload_target_mb * 1024 * 1024 * 8 / max(total_sec, 1)
    video_bps = int(max(500_000, total_bps - 96_000))  # 音声 96kbps 分を差し引く

    fps_norm = f"fps={VIDEO_FPS},settb=AVTB"
    wm_crop = max(0.0, min(float(os.environ.get("WATERMARK_CROP") or "0.08"), 0.3))

    # 各素材を正規化 → [v0]..[v(n-1)]
    norm_parts = []
    for i in range(n):
        if kinds[i] == "clip":
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
        norm_parts.append(chain + f"[v{i}];")

    # ── イントロ実装: 1つ目の素材を「ぼかし+タイトル 3秒 → 通常画」に分割 ──
    labels = [f"[v{i}]" for i in range(n)]
    intro_parts = []
    if use_intro:
        intro_parts.append("[v0]split=2[v0a][v0b];")
        intro_parts.append(f"[v0a]{_title_intro_filter(fps_norm, title_file)}[tseg];")
        intro_parts.append(f"[v0b]{fps_norm},trim={TITLE_INTRO_SEC:.3f},setpts=PTS-STARTPTS[rseg];")
        labels = ["[tseg]", "[rseg]"] + labels[1:]

    concat_in = "".join(labels)
    filter_complex = (
        "".join(norm_parts)
        + "".join(intro_parts)
        + f"{concat_in}concat=n={len(labels)}:v=1:a=0[v];"
        + f"[v]subtitles={clean_srt_path}:force_style='{_subtitle_style()}'[outv]"
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

        # ── CapCut などで編集できるように「素の合成」も書き出す ──
        # 字幕（subtitles.srt）・タイトル（title.txt）は焼き込まず、
        # クリップ＋音声だけの clean_output.mp4 を追加生成する。
        # 字幕・色・タイトルは CapCut 側で自由に直せる（SRT は CapCut にインポート可能）。
        clean_path = os.path.join(output_dir, "clean_output.mp4")
        plain_concat_in = "".join(f"[v{i}]" for i in range(n))
        filter_clean = (
            "".join(norm_parts)
            + f"{plain_concat_in}concat=n={n}:v=1:a=0[vc];"
        )
        cmd2 = ["ffmpeg", "-y"]
        for i, asset_path in enumerate(scene_files):
            if kinds[i] == "clip":
                cmd2 += ["-stream_loop", "-1", "-t", f"{durs[i]:.3f}", "-i", asset_path]
            else:
                cmd2 += ["-loop", "1", "-t", f"{durs[i]:.3f}", "-i", asset_path]
        cmd2 += ["-i", audio_path]
        cmd2 += [
            "-filter_complex", filter_clean,
            "-map", "[vc]",
            "-map", f"{n}:a",
            "-c:v", "libx264",
            "-b:v", str(video_bps),
            "-maxrate", str(int(video_bps * 1.4)),
            "-bufsize", str(int(video_bps * 2.5)),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "96k",
            "-shortest",
            clean_path,
        ]
        subprocess.run(cmd2, check=True)
        print(f"🧩 素材版動画生成完了: {clean_path}")

        # 4. 生成された仮組み動画を Discord に投稿
        msg = (
            f"🎬 **【進捗通知】仮組み動画（draft_output.mp4）の合成が完了しました！**\n"
            f"- **素材モード:** {'手動クリップ' if mode == 'clip' else '自動画像'}\n"
            f"- **動画長:** {total_sec:.2f} 秒\n"
            f"- **シーン数:** {n}"
            + (f"\n- **イントロ:** 先頭 {TITLE_INTRO_SEC} 秒にタイトル「{title_text[:20]}」を表示" if use_intro else "")
            + f"\n- **CapCut編集用:** clean_output.mp4（字幕・タイトルなし）＋ subtitles.srt ＋ title.txt も同梱（Artifacts の zip からDL）"
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
