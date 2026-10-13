"""Wikipedia の歴史記事選定 → Gemini 台本作成 → Discord フォーラムへ投稿

流れ:
  1. 記事を選ぶ(カテゴリ→全文取得→機械的に絞り込み)
  2. Gemini が台本を「小学生向けです・ます調」「1文ずつ・根拠の引用つき」で作成
  3. 作成した台本と操作用ボタン（コピー/原文確定/ファクトチェック）を Discord フォーラムへ投稿
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
    r"|^(Ancient|Medieval|Early modern|Modern) [a-z]+\b"
    r"| in (the )?(ancient|medieval|early modern)\b|(period|era|age|ages)$\vert{}^Middle Ages$",
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

【語り口・トンマナ（最重要ルール）】
・文末はすべて親しみやすい「です・ます調」（「〜です」「〜ます」「〜でしょうか？」）で統一してください。
・小学生（10〜12歳）にもスッと頭に入るよう、難しい歴史用語・専門用語は噛み砕いたやさしい表現に変換してください。
  （例: 重装騎兵→本物の騎士／火器→鉄砲や大砲／騎馬演舞・カルーゼル→馬のパレードやダンス）

【STEP 1: 物語の軸と、「問い」と「答え」を決める(最重要)】
原文から、次のどれか「ひとつだけ」を軸に選びます。
  ・ひとりの人物の運命  ・ひとつの出来事  ・ひとつの建物や物  ・ひとつの謎や疑問
台本のすべての文は、この軸に関係していなければなりません。
原文が、概説・一覧・年表のようで、軸をひとつに絞れない場合は、台本を書かず narrative_ok を false にしてください。

次に、この軸の面白さを「問い」の形にして、その「答え」を、先に原文から探します。
  ・答えが書かれた文を、原文から一字一句そのまま写して、plan.answer_quote に入れる。
  ・「なぜ？」(原因・理由)と問うなら、その原因・理由が、原文にはっきり書かれていることを確認する。
  ・答えが原文に見つからないときは、narrative_ok を false にする。
  ・answer_quote の内容は、「転」か「結」の fact の文として台本に必ず入れる。

【STEP 2: 起承転結で、テンポ良くつなげて書く】
各文に part("起","承","転","結")を付けます。
  起: 軸についての、謎や意外な事実で、視聴者が「どうしてでしょうか？」と思う疑問を立てる(1文)。
  承: その疑問の背景(登場人物・場所・状況)を具体的に語る(fact: 2〜3文)。
  転: 状況が変わる出来事・意外な事実を語る(fact: 2〜3文)。「ですが」「実は」などでつなぐ。
  結: 疑問への答え(answer_quote の内容)と結末を語り(fact: 1〜2文)、短い感想や問いかけで締める(opinion: 1文)。

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

【長さ・テンポ（厳密遵守）】
・台本全体で【300〜340文字】（読み上げ30〜40秒）。
・1文は【35文字以内】でリズム良く書いてください。
・文字数が足りない場合は、別の話題を足さず、軸に沿った具体描写（誰が・どこで・何を）を原文から拾って増やしてください。

【構成の例(中身は真似しないこと。型と「です・ます調」の参考)】
  起(opinion): 「〇〇は、なぜ△△だったのでしょうか？」
  承(fact): 「〇〇は、□□で△△をしていました。」「そこでは、◇◇も行われていたのです。」
  転(fact): 「ですが、△△の年に◇◇が起きます。」「実は、◇◇には〜といった理由がありました。」
  結(fact→opinion): 「こうして、〇〇は△△になっていったのです。」「とても驚きですよね。みなさんはどう思いますか？」

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


def call_groq_json_fallback(prompt):
    """Gemini 全滅時の Groq フォールバック処理"""
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY が設定されていないため Groq フォールバックをスキップします。")

    log("🚀 Gemini 全滅のため、Groq (llama-3.3-70b-versatile) へフォールバックを実行します...")
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": "あなたは優秀な歴史雑学ショート動画の台本作家です。返答は解説などの余計なテキストを含めず、指定されたJSONオブジェクトのみを出力してください。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
    }

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=HTTP_TIMEOUT_SEC)
        
        # 💡 エラーが発生した場合、Groq が返した生の応答本文をログに出力する
        if not res.ok:
            log(f"❌ Groq API エラー詳細 ({res.status_code}): {res.text}")
            res.raise_for_status()

        reply_content = res.json()["choices"][0]["message"]["content"]
        
        # 💡 レスポンスから JSON 部分だけを正規表現で切り出し
        json_match = re.search(r"\{[\s\S]*\}", reply_content)
        if json_match:
            data = json.loads(json_match.group(0))
        else:
            data = json.loads(reply_content)

        if isinstance(data, dict):
            log("✅ Groq (llama-3.3-70b-versatile) で台本作成に成功しました！")
            return data

    except Exception as e:
        log(f"❌ Groq API フォールバック処理で例外発生: {e}")
        raise RuntimeError(f"Groq 呼び出し例外: {e}")


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
