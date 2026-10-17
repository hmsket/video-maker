"""Wikipedia の歴史記事選定 → Gemini 台本作成 → Discord フォーラムへ投稿

流れ:
  1. 記事を選ぶ(カテゴリ→全文取得→機械的に絞り込み)
  2. Gemini が台本を「小学生向けです・ます調」「1文ずつ・根拠の引用つき」で作成
  3. 作成した台本と操作用ボタン（コピー/原文確定/ファクトチェック）を Discord フォーラムへ投稿
     ※ 動画生成用メタデータ(元台本・プロンプト・出典のJSON)もスレッドに投稿する
  4. 後続の処理用に output/trivia_result.json を保存 (スレッドID追記)

必要な環境変数: DISCORD_TOKEN, FORUM_CHANNEL_ID, GEMINI_API_KEY
任意: GROQ_API_KEY, GEMINI_MODELS, WIKI_CATEGORIES, MAX_ARTICLES
"""

import json
import os
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

START = time.time()


def log(*args):
    """経過時間つきで、すぐに表示する(GitHub Actions 出力用)。"""
    t = int(time.time() - START)
    print(f"[{t // 60:02d}:{t % 60:02d}]", *args, flush=True)


# ------------------------------------------------------------------ 設定
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

try:
    FORUM_CHANNEL_ID = int(os.environ.get("FORUM_CHANNEL_ID", "0").strip())
except ValueError:
    log("❌ FORUM_CHANNEL_ID の数値変換に失敗しました。")
    FORUM_CHANNEL_ID = 0


def _env_list(name, default):
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


GEMINI_MODELS = _env_list(
    "GEMINI_MODELS",
    "gemini-3.8-flash, gemini-3.6-flash, gemini-3.5-flash-lite, gemini-3.5-flash",
)
CATEGORIES = _env_list(
    "WIKI_CATEGORIES",
    "Category:Early_modern_history,Category:Ancient_history,Category:Middle_Ages",
)
MAX_RUNTIME_SEC = int(os.environ.get("MAX_RUNTIME_SEC", "300"))
HTTP_TIMEOUT_SEC = int(os.environ.get("HTTP_TIMEOUT_SEC", "60"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")
MAX_ARTICLES = int(os.environ.get("MAX_ARTICLES", "4"))
MAX_SOURCE_CHARS = int(os.environ.get("MAX_SOURCE_CHARS", "30000"))
MIN_ARTICLE_CHARS = int(os.environ.get("MIN_ARTICLE_CHARS", "3000"))

EXCLUDE_TITLE_REGEX = os.environ.get(
    "EXCLUDE_TITLE_REGEX",
    r"^(History|Economy|Culture|Society|Religion|Geography|Demographics|Politics|Architecture|Art|Music|Science|Law|Economic history|Military history) of\b"
    r"| in (the )?(ancient|medieval|early modern)\b|(period|era|age|ages)$"
    r"|^Middle Ages$",
)

WIKI_API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "VideoMakerBot/1.1 (https://github.com/hmsket/video-maker)"}


def out_of_time():
    if time.time() - START > MAX_RUNTIME_SEC:
        log(f"⏱️ 制限時間({MAX_RUNTIME_SEC}秒)を超えたため、打ち切ります。")
        return True
    return False


def extract_json(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ------------------------------------------------------------------ 1. 記事選定
def api_get(params, retries=3):
    p = {"format": "json", "formatversion": 2}
    p.update(params)
    last = None
    for i in range(retries):
        try:
            r = requests.get(WIKI_API, params=p, headers=HEADERS, timeout=(5, 20))
            if r.status_code in (429, 500, 502, 503, 504):
                wait = min(int(r.headers.get("Retry-After", 2 * (i + 1))), 15)
                log(f"    ⚠️ Wikipedia が {r.status_code} を返しました。{wait}秒待って再試行します")
                time.sleep(wait)
                last = RuntimeError(f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
            return r.json()
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
            log(f"    ⚠️ Wikipedia への接続エラー({type(e).__name__})。再試行します")
            time.sleep(2)
    raise RuntimeError(f"Wikipedia API が使えません: {last}")


def list_category_titles(category, depth=2):
    d = api_get({
        "action": "query",
        "list": "categorymembers",
        "cmtitle": category,
        "cmtype": "page|subcat",
        "cmlimit": 200,
    })
    members = d.get("query", {}).get("categorymembers", [])
    pages = [m["title"] for m in members if m.get("ns") == 0]
    subcats = [m["title"] for m in members if m.get("ns") == 14]
    if len(pages) < 5 and subcats and depth > 0:
        sub = random.choice(subcats)
        log(f"    ↳ サブカテゴリへ: {sub}")
        pages += list_category_titles(sub, depth - 1)
    return pages


def looks_pre_1900(text):
    lead = text[:3000]
    if re.search(r"\b\d{1,4}\s*(BC|BCE)\b", lead):
        return True
    years = [int(y) for y in re.findall(r"\b(\d{3,4})\b", lead)]
    years = [y for y in years if 100 <= y <= 2100]
    old = [y for y in years if y < 1900]
    new = [y for y in years if y >= 1900]
    return len(old) >= 1 and len(new) <= len(old)


def check_article_suitable(article):
    title, text = article["title"], article["text"]
    if title.startswith(("List of", "Timeline of", "Outline of", "Index of")):
        return False, "一覧・年表系の記事"
    if re.search(EXCLUDE_TITLE_REGEX, title, re.I):
        return False, "概説系のタイトル"
    if article.get("disambiguation"):
        return False, "曖昧さ回避ページ"
    if article.get("living"):
        return False, "存命人物の記事"
    if len(text) < MIN_ARTICLE_CHARS:
        return False, f"本文が短い({len(text)}字)"
    if not looks_pre_1900(text):
        return False, "1900年より前の話に見えない"
    return True, "OK"


def fetch_article(title):
    d = api_get({
        "action": "query",
        "titles": title,
        "redirects": 1,
        "prop": "extracts|info|categories|pageprops",
        "explaintext": 1,
        "exsectionformat": "plain",
        "inprop": "url",
        "cllimit": "max",
        "clcategories": "Category:Living people",
        "ppprop": "disambiguation",
    })
    pages = d.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return None
    p = pages[0]
    text_full = (p.get("extract") or "").strip()
    return {
        "title": p["title"],
        "fullurl": (
            p.get("fullurl")
            or f"https://en.wikipedia.org/wiki/{p['title'].replace(' ', '_')}"
        ),
        "revid": p.get("lastrevid"),
        "text": text_full[:MAX_SOURCE_CHARS],
        "living": bool(p.get("categories")),
        "disambiguation": "disambiguation" in (p.get("pageprops") or {}),
    }


def pick_article():
    for _ in range(8):
        if out_of_time():
            return None
        cat = random.choice(CATEGORIES)
        log(f"🔍 カテゴリ: {cat}")
        try:
            titles = list_category_titles(cat)
        except Exception as e:
            log(f"    ⚠️ カテゴリ取得エラー: {e}")
            continue
        random.shuffle(titles)
        for t in titles[:6]:
            try:
                art = fetch_article(t)
            except Exception as e:
                log(f"    ⚠️ 記事取得エラー({t}): {e}")
                continue
            if not art:
                continue
            ok, why = check_article_suitable(art)
            log(f"    {'✅' if ok else '✖'} {art['title']}  → {why}")
            if ok:
                return art
    return None


# ------------------------------------------------------------------ 2. 台本生成 (Gemini & Groq Fallback)
SCRIPT_PROMPT = """あなたは、小学生向けの歴史雑学ショート動画の台本作家です。
下の【原文】だけを材料に、YouTube Shorts / TikTok 用(読み上げ約30〜40秒)のナレーション台本を書いてください。
目標は「事実の羅列」ではなく、「小学生でもワクワクしながら最後まで理解できる、テンポの良いショート物語」です。

【言葉の易しさとトーン（最重要ルール）】
・文末はすべて親しみやすい「です・ます調」（「〜です」「〜ます」「〜でしょうか？」）で統一してください。
・小学生（10〜12歳）にもスッと頭に入るよう、難しい歴史用語・専門用語は噛み砕いたやさしい表現に変換してください。
  （例: 重装騎兵→本物の騎士／火器→鉄砲や大砲／騎馬演舞・カルーセル→馬のパレードやダンス）
・漢字は小学校中・高学年で読めるものが目安。読みづらい漢字・難しい漢字は平仮名に開いてください。
・1文のなかの固有名詞（人名・地名・建物名・国名）は1つだけ。知らない言葉が続くと、内容が頭に入らなくなります。
・ぼんやりした数量は、目に浮かぶ具体的な形で伝えます（「多くの」→「なんと約3,000人」など）。ただし、【原文】に書かれていない数字は絶対に使わないでください。

【STEP 1: 物語の軸と、「問い」と「答え」を決める(最重要)】
原文から、次のどれか「ひとつだけ」を軸に選びます。
  ・ひとりの人物の運命  ・ひとつの出来事  ・ひとつの建物や物  ・ひとつの謎や疑問
台本のすべての文は、この軸に関係していなければなりません。
・軸を複数の候補から選べる場合は、「視聴者の予想から一番遠いもの」を選んでください。誰もがイメージする常識と真逆の展開を語れる軸こそが、最後まで見たくなる物語になります。
・軸にできる「びっくりするような事実」が原文に見当たらないときは、あえて無理をせず narrative_ok を false にしてください。
原文が、概説・一覧・年表のようで、軸をひとつに絞れない場合は、台本を書かず narrative_ok を false にしてください。

次に、この軸の面白さを「問い」の形にして、その「答え」を、先に原文から探します。
  ・答えが書かれた文を、原文から一字一句そのまま写して、plan.answer_quote に入れる。
  ・「なぜ？」(原因・理由)と問うなら、その原因・理由が、原文にはっきり書かれていることを確認する。
  ・答えが原文に見つからないときは、narrative_ok を false にする。
  ・answer_quote の内容は、「転」か「結」の fact の文として台本に必ず入れる。

【STEP 2: 起承転結で、テンポ良くつなげて書く】
各文に part("起","承","転","結")を付けます。
  起: 冒頭1文で、視聴者の予想と真逆の事実、または強い「なぜ？」を置き、最後まで離脱できない問いを立てる(opinion: 1文)。
  承: 登場人物・場所・状況が目に浮かぶように「誰が・どこで・何を」を具体的に語る(fact: 2〜3文)。原文にある数字（人数・年・長さなど）を使うと画面が生きます。
  転: ここが物語の一番面白い瞬間。視聴者の予想をひっくり返る出来事・意外な事実を置く(fact: 2〜3文)。「ところが」「実は」などの一言葉を使ってください。
  結: 冒頭の問いの答え(answer_quote の内容)をはっきり語り(fact: 1〜2文)、「思ったのとは違いましたね」のような感情の動きを伴う短い感想や問いかけ(opinion: 1文)で余韻を残して締める。

【ファクトチェックを通過するための重要注意事項】
・単一の原因に限定して断定しないでください（❌「一番の理由は〜」「〜のせいで」）。
  「〜といった理由がありました」「〜など、さまざまな要因が重なりました」のように柔軟な表現にしてください。
・因果関係を直接的に断定しすぎないでください（例: 「〜へと形を変えながら受け継がれていきました」のように史実に忠実にぼかす）。
・因果の言葉(「その結果」「そのため」「だから」)は、原文の引用が明らかな因果関係(because, due to, as a result, led to, caused など)を述べているときだけ使用可能です。

【STEP 3: 事実と感想の書き分け(ルール)】
・台本は「1文ずつ」に分け、各文に type を付けます。
  - "fact": 原文に書かれた事実を述べる文(全体で5〜6文)。必ず evidence_quote を付ける。
  - "opinion": 問いかけ・感想・まとめの文(全体で2〜3文)。evidence_quote は null。
・fact の evidence_quote は、【原文】から一字一句そのまま写した英語の連続した引用(15〜300文字)。
・fact の text に入れてよい内容は、その引用が直接裏付けるものだけです。

【納得水準の最終チェック（1つでも満たせなければ書き直してから出力）】
・冒頭の1文だけで、最後まで見たくなるか（予想と真逆の事実、または強い「なぜ？」があるか）。
・小さな子に読み聞かせして、全部が伝わるか（調べる必要のある言葉や漢字が残っていないか）。
・「転」で、視聴者の予想がひっくり返るか（ただの事実の説明で流れていないか）。
・最後の1文で、答えの「おどろき」を実感として受け取れるか（感情の動きや問いかけが残るか）。

【長さ・テンポ（厳密遵守）】
・台本全体で【300〜340文字】（読み上げ30〜40秒）。
・1文は【35文字以内】でリズム良く書いてください。
・文字数が足りない場合は、別の話題を足さず、軸に沿った具体描写（誰が・どこで・何を）を原文から拾って増やしてください。

【構成の例(中身は真似しないこと。型と「です・ます調」の参考)】
  起(opinion): 「あの有名な〇〇は、実は、みなさんの想像する△△とは真逆のものです。では、なぜでしょうか？」
  承(fact): 「〇〇は、□□の年に◇◇として姿を現しました。」「当時の記録では、▢▢という様子が伝わっています。」
  転(fact): 「ところが、その後、〇〇の使われ方は正反対のものへと大きく変わります。」「実はその裏には、▣▣という事情が隠れていました。」
  結(fact→opinion): 「こうして、〇〇は最後に△△として残ったのです。」「最初のイメージとは違いましたね。みなさんはどう感じましたか？」

{feedback}
【記事タイトル】{title}
【原文】
{text}

次の JSON だけを返してください(narrative_ok が false のときは、sentences は空でよい):
{
  "narrative_ok": true,
  "reason_if_not": "false のときだけ、軸を絞れない理由",
  "plan": {"axis": "物語の軸(1文)", "hook_question": "起で立てる疑問", "payoff": "結で回収する答え(日本語)",
           "answer_quote": "答えが書かれた、原文からの英語の引用(一字一句そのまま)"},
  "title": "惹きつける日本語タイトル(20文字以内)",
  "summary": "原文の事実だけによる概要(120文字程度)",
  "sentences": [
    {"text": "日本語の1文", "part": "起", "type": "opinion", "evidence_quote": null},
    {"text": "日本語の1文", "part": "承", "type": "fact", "evidence_quote": "原文からの英語の引用"}
  ],
  "image_prompts": ["英語の画像プロンプト4つ(歴史画風、文字なし、残酷な描写なし)"],
  "video_prompts": ["英語のカメラワーク指示4つ"]
}
"""


class NarrativeUnsuitable(Exception):
    pass


def call_gemini_json(prompt):
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=HTTP_TIMEOUT_SEC * 1000),
    )
    last = None
    for model in GEMINI_MODELS:
        if out_of_time():
            break
        try:
            log(f"🤖 Gemini ({model}) で台本を作成中...")
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"
                ),
            )
            data = json.loads(resp.text.strip())
            if isinstance(data, dict):
                return data
        except Exception as e:
            last = e
            log(f"⚠️ {model}: {e}")
            wait = 5 if ("429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)) else 2
            time.sleep(wait)
    raise RuntimeError(f"Gemini の呼び出しにすべて失敗しました: {last}")


GROQ_MODELS = _env_list("GROQ_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b")

GROQ_SYSTEM_HINT = (
    "あなたは優秀な歴史雑学ショート動画の台本作家です。"
    "出力は { で始まり } で終わる、指定されたキーを持つ JSON オブジェクト**だけ**にしてください。"
    "前置き・解説・コードブロック・ [[]] などの囲みは絶対に付けないこと。"
)


def list_groq_models():
    """このキーで実際に使えるモデルID一覧(404のとき、ログに出して原因を確認するため)。"""
    try:
        r = requests.get("https://api.groq.com/openai/v1/models",
                         headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]
    except Exception as e:
        log(f"⚠️ Groq のモデル一覧を取得できませんでした: {e}")
        return []


def call_groq_json_fallback(prompt):
    """Gemini 全滅時の Groq フォールバック。モデルを順に試す。

    Groq の JSON 強制モード (response_format: json_object) は、長大なプロンプトだと
    「Failed to generate JSON (400)」で落ちることがある。その場合は
    モードを外して再試行し、応答テキストから extract_json() で JSON を拾う。
    """
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY が未設定のため、Groq フォールバックをスキップします。")

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    last = None
    listed = False
    for model in GROQ_MODELS:
        if out_of_time():
            break
        # 1回目: JSON 強制モードあり → 400 で失敗したら 2回目: モードなしで再試行
        for use_json_mode in (True, False):
            if out_of_time():
                break
            mode_label = "json_mode" if use_json_mode else "no_format"
            log(f"🚀 Groq ({model}, {mode_label}) で台本を作成中...")
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": GROQ_SYSTEM_HINT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
            }
            if use_json_mode:
                payload["response_format"] = {"type": "json_object"}
            if "gpt-oss" in model:
                payload["reasoning_effort"] = "low"
            try:
                res = requests.post(url, headers=headers, json=payload, timeout=HTTP_TIMEOUT_SEC)
                if not res.ok:
                    log(f"❌ Groq ({model}, {mode_label}) HTTP {res.status_code}: {res.text[:300]}")
                    if res.status_code in (400, 404) and not listed:
                        listed = True
                        log(f"ℹ️ このキーで使えるモデル: {', '.join(list_groq_models()) or '(取得失敗)'}")
                    last = RuntimeError(f"HTTP {res.status_code}")
                    if res.status_code == 429:
                        time.sleep(5)
                    continue  # JSONモードなしで再試行（次ラウンド）
                data = extract_json(res.json()["choices"][0]["message"]["content"])
                if isinstance(data, dict):
                    log(f"✅ Groq ({model}, {mode_label}) で台本作成に成功しました")
                    return data
                last = RuntimeError("JSON を取り出せませんでした")
                log(f"⚠️ Groq ({model}, {mode_label}): {last}")
            except Exception as e:
                last = e
                log(f"⚠️ Groq ({model}, {mode_label}): {e}")
    raise RuntimeError(f"Groq の呼び出しにすべて失敗しました: {last}")


def generate_draft(article):
    prompt = SCRIPT_PROMPT.replace("{title}", article["title"]).replace(
        "{text}", article["text"]
    ).replace("{feedback}", "")

    d = None
    try:
        d = call_gemini_json(prompt)
    except Exception as gemini_err:
        log(f"⚠️ {gemini_err}")
        try:
            d = call_groq_json_fallback(prompt)
        except Exception as groq_err:
            raise RuntimeError(f"すべての AI モデル (Gemini/Groq) での台本生成に失敗しました: {groq_err}")

    if d.get("narrative_ok") is False:
        raise NarrativeUnsuitable(d.get("reason_if_not") or "軸を絞れない記事")
    sents = d.get("sentences")
    if not isinstance(sents, list) or not sents:
        raise ValueError("sentences がありません")
    d["sentences"] = [s for s in sents if isinstance(s, dict)]
    d.setdefault("title", article["title"][:20])
    d.setdefault("summary", "")
    d.setdefault("image_prompts", [])
    d.setdefault("video_prompts", [])
    return d


def script_text(sentences):
    return "".join(s["text"].strip() for s in sentences)


# ------------------------------------------------------------------ 3. Discord 投稿 & 保存
def build_meta_message(article, data):
    """動画生成パイプライン用のメタデータ(JSON)をコードブロック付きで作成する。

    prepare_trivia.py が後からスレッドを読み、修正台本以外の
    (元台本・タイトル・概要・画像/動画プロンプト・出典)をここから復元する。
    """
    meta = {
        "type": "video_maker_meta",
        "title": data.get("title"),
        "summary": data.get("summary"),
        "script_text": script_text(data["sentences"]),
        "image_prompts": data.get("image_prompts", []),
        "video_prompts": data.get("video_prompts", []),
        "article": {
            "title": article["title"],
            "url": article.get("fullurl"),
            "revid": article.get("revid"),
            "license": "CC BY-SA 4.0",
        },
    }
    # Discord の 2000 文字制限を回避するため圧縮した JSON にする
    body = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    return (
        "⚙️ **【生成メタデータ】** 動画生成パイプライン用の内部データです。編集は不要です。\n"
        f"```json\n{body}\n```"
    )


def build_messages(article, data):
    """概要メッセージと、コードブロック単体の台本メッセージを2つ作成する。"""
    jst = timezone(timedelta(hours=9))
    accessed = datetime.now(jst).strftime("%Y/%m/%d %H:%M")
    sents = data["sentences"]
    pure_script = script_text(sents)

    # 1. 概要・出典・案内メッセージ
    info_msg = (
        f"**【概要】**\n{data['summary']}\n\n"
        f"📚 **【出典情報】**\n"
        f"・原題: `{article['title']}`\n"
        f"・URL: <{article['fullurl']}>\n"
        f"・ライセンス: CC BY-SA 4.0\n"
        f"・閲覧日時: {accessed} (JST)\n\n"
        f"--- \n"
        f"✏️ 以下のコードブロックをコピーして修正後、返信投稿してください。\n"
        f"投稿にスタンプ（リアクション）を付けて `/fact` と入力するとファクトチェックが実行されます。"
    )

    # 2. 台本のみのメッセージ（コピー専用）
    script_msg = f"{pure_script}"

    return info_msg, script_msg


def save_trivia_json(article, data, thread_id=None):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = {
        "thread_id": str(thread_id) if thread_id else None,
        "title": data.get("title", article["title"]),
        "summary": data.get("summary", ""),
        "script_text": script_text(data["sentences"]),
        "sentences": data["sentences"],
        "image_prompts": data.get("image_prompts", []),
        "video_prompts": data.get("video_prompts", []),
        "status": "generated",
        "article": {
            "title": article["title"],
            "url": article.get("fullurl"),
            "revid": article.get("revid"),
            "license": "CC BY-SA 4.0",
        },
    }
    path = os.path.join(OUTPUT_DIR, "trivia_result.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    log(f"💾 JSON を保存しました: {path} (thread_id={out['thread_id']})")


def post_to_discord(article, data):
    import discord

    info_msg, script_msg = build_messages(article, data)
    meta_msg = build_meta_message(article, data)
    result = {"ok": False}

    class Poster(discord.Client):

        async def on_ready(self):
            log(f"🤖 Discord Bot ログイン完了: {self.user}")
            try:
                ch = self.get_channel(FORUM_CHANNEL_ID) or await self.fetch_channel(
                    FORUM_CHANNEL_ID
                )
                if not isinstance(ch, discord.ForumChannel):
                    log("❌ フォーラムチャンネルが見つかりませんでした。")
                else:
                    # 1. 概要・出典情報でスレッドを作成
                    t = await ch.create_thread(
                        name=f"📜 {data['title']}"[:100], content=info_msg
                    )
                    thread = t.thread

                    # 2. コードブロック単体のメッセージを連続投稿（コピー用）
                    await thread.send(script_msg)

                    # 3. 動画生成用メタデータ(JSON)を投稿（prepare_trivia.py が後から読む）
                    await thread.send(meta_msg)

                    save_trivia_json(article, data, thread.id)
                    log(
                        f"🎉 投稿完了: {thread.name} (Thread ID: {thread.id})"
                    )
                    result["ok"] = True
            except Exception as e:
                log(f"❌ Discord 投稿エラー: {e}")
            finally:
                await self.close()

    intents = discord.Intents.default()
    intents.guilds = True
    Poster(intents=intents).run(DISCORD_TOKEN)
    return result["ok"]


# ------------------------------------------------------------------ main
def main():
    if not GEMINI_API_KEY or not DISCORD_TOKEN or not FORUM_CHANNEL_ID:
        log("❌ 必須環境変数が設定されていません。")
        return 1

    for n in range(1, MAX_ARTICLES + 1):
        if out_of_time():
            break
        log(f"\n===== 記事選定 & 台本作成 {n}/{MAX_ARTICLES} =====")
        article = pick_article()
        if not article:
            continue

        log(f"📖 記事: {article['title']}")
        try:
            data = generate_draft(article)
        except NarrativeUnsuitable as e:
            log(f"↪ 見送り: {e}")
            continue
        except Exception as e:
            log(f"⚠️ 台本生成エラー: {e}")
            continue

        if data:
            save_trivia_json(article, data, None)
            log("🚀 Discord フォーラムへ投稿します...")
            if post_to_discord(article, data):
                return 0

    log("❌ 台本を作成・投稿できませんでした。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
