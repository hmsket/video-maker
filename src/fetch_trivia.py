"""Wikipedia の歴史記事 → 根拠つき台本 → Discord フォーラムへ投稿

流れ:
  1. 記事を選ぶ(カテゴリ→全文取得→「歴史ネタとして使えるか」を機械的に絞り込む)
  2. Gemini が台本を「1文ずつ・根拠の引用つき」で書く
  3. 機械照合: 各文の引用が原文に本当にあるか / 数字が引用に含まれるか
  4. Groq の意味チェック: 引用が、その文の内容を本当に裏付けているか
  5. 問題があれば、指摘つきで再生成。それでも残る文は削除
  6. 人が確認しやすいように、根拠一覧つきで Discord に投稿
  7. 後続の音声合成用に output/trivia_result.json を出力(投稿前に保存→投稿後にスレッドIDを追記)

必要な環境変数: DISCORD_TOKEN, FORUM_CHANNEL_ID, GEMINI_API_KEY, GROQ_API_KEY(任意)
任意: GEMINI_MODELS, GROQ_MODELS, WIKI_CATEGORIES(カンマ区切り), MAX_ARTICLES
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
    """経過時間つきで、すぐに表示する(GitHub Actions は、出力がたまってから出るため)。"""
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


GEMINI_MODELS = _env_list("GEMINI_MODELS", "gemini-3.8-flash, gemini-3.6-flash, gemini-3.5-flash-lite, gemini-3.5-flash")
# Groq は廃止が早いので、起動時に list_groq_models() で確認するのがおすすめ
GROQ_MODELS = _env_list("GROQ_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b")
CATEGORIES = _env_list(
    "WIKI_CATEGORIES",
    "Category:Early_modern_history,Category:Ancient_history,Category:Middle_Ages",
)
MAX_RUNTIME_SEC = int(os.environ.get("MAX_RUNTIME_SEC", "600"))        # 全体の制限時間
HTTP_TIMEOUT_SEC = int(os.environ.get("HTTP_TIMEOUT_SEC", "60"))       # LLM 1回の待ち時間の上限
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "output")
MAX_ARTICLES = int(os.environ.get("MAX_ARTICLES", "4"))   # 記事を選び直す回数の上限
MAX_ATTEMPTS = 3                                          # 1記事あたりの台本再生成の上限
MAX_SOURCE_CHARS = int(os.environ.get("MAX_SOURCE_CHARS", "30000"))   # LLM に渡す原文の上限(長い記事は、原因が後ろにあることが多い)
MIN_ARTICLE_CHARS = int(os.environ.get("MIN_ARTICLE_CHARS", "3000"))  # 短い記事は材料不足として捨てる
MIN_QUOTE_CHARS = 15
MAX_QUOTE_CHARS = 300
MIN_SCRIPT_CHARS = 300                                    # 検証後の台本がこれより短ければ捨てる
MIN_FACT_SENTENCES = 5
MIN_TARGET_CHARS = int(os.environ.get("MIN_TARGET_CHARS", "300"))   # これ未満は再生成(最後は通す)
PARTS = "起承転結"
# 因果を表す日本語が、事実の文にあるのに、引用に因果の記述がなければ不合格にする
CAUSAL_JA = re.compile(r"その結果|そのため|だから|したがって|このため|のせいで|が原因|が理由|というわけで")
CAUSAL_EN = re.compile(r"\b(because|due to|as a result|resulted?|resulting|result in|caused?|causing|led to|leading to|"
                       r"therefore|thus|consequently|owing to|reasons?|so that|since|hence|thanks to)\b", re.I)

# 概説・一覧のような記事は、物語にしにくいので除外する(環境変数 EXCLUDE_TITLE_REGEX で変更可)
EXCLUDE_TITLE_REGEX = os.environ.get(
    "EXCLUDE_TITLE_REGEX",
    r"^(History|Economy|Culture|Society|Religion|Geography|Demographics|Politics|Architecture|Art|Music|Science|Law|Economic history|Military history) of\b"
    r"|^(Ancient|Medieval|Early modern|Modern) [a-z]+\b"
    r"| in (the )?(ancient|medieval|early modern)\b|(period|era|age|ages)$|^Middle Ages$",
)

WIKI_API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "VideoMakerBot/1.1 (https://github.com/hmsket/video-maker)"}


def out_of_time():
    if time.time() - START > MAX_RUNTIME_SEC:
        log(f"⏱️ 制限時間({MAX_RUNTIME_SEC}秒)を超えたため、打ち切ります。")
        return True
    return False


# ------------------------------------------------------------------ 文字列ユーティリティ
def norm(s):
    """引用の照合用に、表記ゆれをそろえる。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[‐-―−]", "-", s)
    return re.sub(r"\s+", " ", s).strip().lower()


_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")


def numbers(s):
    s = unicodedata.normalize("NFKC", s or "")
    return {n.replace(",", "") for n in _NUM_RE.findall(s)}


def extract_json(text):
    """LLM の返答から、最初の { 〜 最後の } を取り出して JSON として読む。"""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ------------------------------------------------------------------ 1. 記事選び
def api_get(params, retries=3):
    p = {"format": "json", "formatversion": 2}
    p.update(params)
    last = None
    for i in range(retries):
        try:
            r = requests.get(WIKI_API, params=p, headers=HEADERS, timeout=(5, 20))
            if r.status_code in (429, 500, 502, 503, 504):
                wait = min(int(r.headers.get("Retry-After", 2 * (i + 1))), 15)
                log(f"   ⚠️ Wikipedia が {r.status_code} を返しました。{wait}秒待って再試行します")
                time.sleep(wait)
                last = RuntimeError(f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
            return r.json()
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
            log(f"   ⚠️ Wikipedia への接続エラー({type(e).__name__})。再試行します")
            time.sleep(2)
    raise RuntimeError(f"Wikipedia API が使えません: {last}")


def list_category_titles(category, depth=2):
    """カテゴリ内の記事タイトルを返す。直下に記事が少なければ、サブカテゴリへ降りる。"""
    d = api_get({"action": "query", "list": "categorymembers", "cmtitle": category,
                 "cmtype": "page|subcat", "cmlimit": 200})
    members = d.get("query", {}).get("categorymembers", [])
    pages = [m["title"] for m in members if m.get("ns") == 0]
    subcats = [m["title"] for m in members if m.get("ns") == 14]
    if len(pages) < 5 and subcats and depth > 0:
        sub = random.choice(subcats)
        log(f"   ↳ サブカテゴリへ: {sub}")
        pages += list_category_titles(sub, depth - 1)
    return pages


def looks_pre_1900(text):
    """冒頭に出てくる年から、1900年より前の話っぽいかを判定する(簡易)。"""
    lead = text[:3000]
    if re.search(r"\b\d{1,4}\s*(BC|BCE)\b", lead):
        return True
    years = [int(y) for y in re.findall(r"\b(\d{3,4})\b", lead)]
    years = [y for y in years if 100 <= y <= 2100]
    old = [y for y in years if y < 1900]
    new = [y for y in years if y >= 1900]
    return len(old) >= 1 and len(new) <= len(old)


def check_article_suitable(article):
    """(OKか, 理由) を返す。"""
    title, text = article["title"], article["text"]
    if title.startswith(("List of", "Timeline of", "Outline of", "Index of")):
        return False, "一覧・年表系の記事"
    if re.search(EXCLUDE_TITLE_REGEX, title, re.I):
        return False, "概説系のタイトル(物語にしにくい)"
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
    """記事の全文と、判定用のメタ情報を取得する。"""
    d = api_get({
        "action": "query", "titles": title, "redirects": 1,
        "prop": "extracts|info|categories|pageprops",
        "explaintext": 1, "exsectionformat": "plain", "inprop": "url",
        "cllimit": "max", "clcategories": "Category:Living people",
        "ppprop": "disambiguation",
    })
    pages = d.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        return None
    p = pages[0]
    return {
        "title": p["title"],
        "fullurl": p.get("fullurl") or f"https://en.wikipedia.org/wiki/{p['title'].replace(' ', '_')}",
        "revid": p.get("lastrevid"),
        "text": (p.get("extract") or "").strip()[:MAX_SOURCE_CHARS],
        "truncated": len((p.get("extract") or "").strip()) > MAX_SOURCE_CHARS,
        "living": bool(p.get("categories")),
        "disambiguation": "disambiguation" in (p.get("pageprops") or {}),
    }


def pick_article():
    """条件に合う記事が見つかるまで、カテゴリを変えながら探す。"""
    for _ in range(8):
        if out_of_time():
            return None
        cat = random.choice(CATEGORIES)
        log(f"🔍 カテゴリ: {cat}")
        try:
            titles = list_category_titles(cat)
        except Exception as e:
            log(f"   ⚠️ カテゴリ取得エラー: {e}")
            continue
        random.shuffle(titles)
        for t in titles[:6]:
            try:
                art = fetch_article(t)
            except Exception as e:
                log(f"   ⚠️ 記事取得エラー({t}): {e}")
                continue
            if not art:
                continue
            ok, why = check_article_suitable(art)
            log(f"   {'✅' if ok else '✖'} {art['title']}  → {why}")
            if ok:
                return art
    return None


# ------------------------------------------------------------------ 2. 台本生成(Gemini)
SCRIPT_PROMPT = """あなたは、歴史の雑学ショート動画の台本作家です。
下の【原文】だけを材料に、YouTube Shorts 用(読み上げ約1分)のナレーション台本を書いてください。
目標は「事実の羅列」ではなく、「最後まで聞きたくなる、ひとつの物語」です。

【STEP 1: 物語の軸と、「問い」と「答え」を決める(最重要)】
原文から、次のどれか「ひとつだけ」を軸に選びます。
  ・ひとりの人物の運命  ・ひとつの出来事  ・ひとつの建物や物  ・ひとつの謎や疑問
台本のすべての文は、この軸に関係していなければなりません。
「他にもこんなことがあった」と別の話題に広げたり、同じ種類の事実を並べたりしてはいけません。
原文が、概説・一覧・年表のようで、軸をひとつに絞れない場合は、台本を書かず narrative_ok を false にしてください。
(無理に書いて、事実を寄せ集めた台本にするより、見送るほうが良いです。)

次に、この軸の面白さを「問い」の形にして、その「答え」を、先に原文から探します。
  ・答えが書かれた文を、原文から一字一句そのまま写して、plan.answer_quote に入れる。
  ・「なぜ？」(原因・理由)と問うなら、その原因・理由が、原文にはっきり書かれていることを、必ず確認する。
    書かれていなければ、「なぜ」とは問わず、「何が起きた？」「どれほど？」「誰が？」の形の問いにする。
  ・答えが、原文のどこにも見つからないときは、narrative_ok を false にする。
  ・answer_quote の内容は、「転」か「結」の fact の文として、台本に必ず入れる。
    問いかけたのに、答えがない台本は、不合格です。

【STEP 2: 起承転結で、つなげて書く】
各文に part("起","承","転","結")を付け、4つすべてを使います。
  起: 軸についての、謎や意外な事実で、視聴者が「どうして？」と思う疑問をひとつ立てる(答えが原文にあるものだけ)。
  承: その疑問の背景(登場人物・場所・状況)を、時間の順に、具体的に語る(fact を複数文)。
  転: 状況が変わる出来事・ピンチ・意外な事実を語る(fact を複数文)。「ところが」「しかし」「実は」などでつなぐ。
  結: 疑問への答え(answer_quote の内容)と、結末を fact で語る。そのあと、感想、視聴者への問いかけで締める。
      最初の問いかけに「戻る」形(回収)にすると、まとまりが出ます。
・文と文は、前の文を受けて次の文が続くようにします。「そして」「その後」「ところが」「しかし」「実は」「さて」などの、つなぎ言葉を使ってよい。
・因果の言葉(「その結果」「そのため」「だから」「このため」「〜のせいで」「〜が原因」)は、その文の引用が、因果を英語で述べている
  (because, due to, as a result, led to, caused など)ときだけ使えます。それ以外では使わず、「そして」「その後」でつなぐ。
・語り口は、視聴者に話しかけるように。短い文、具体的な名詞(人・物・場所)で、映像が浮かぶように書く。

【STEP 3: 事実と感想の書き分け(ルール)】
・台本は「1文ずつ」に分け、各文に type を付けます。
  - "fact": 原文に書かれた事実を述べる文(全体で5文以上)。必ず evidence_quote を付ける。
  - "opinion": 問いかけ・感想・まとめの文(全体で2〜3文)。evidence_quote は null。
    ・新しい事実を主張してはいけません(数字・年代は入れない)。「〜かもしれません」「〜と思いませんか？」のようにぼかす。
    ・fact の文に出てくる人物や物の名前を、問いかけに使うのは、かまいません。
    ・感想は「直前の fact への素直な反応(例: それは驚きですよね。)」か「視聴者への問いかけ」の形にします。
・fact の evidence_quote は、【原文】から一字一句そのまま写した、英語の連続した引用(15〜300文字)。
  要約・言い換え・「...」での省略・翻訳をしてはいけません。
・fact の text に入れてよい内容は、その引用が直接裏付けるものだけです。
  - 数字・人名・地名・年代は、引用にあるものだけを使う。アラビア数字で書く(例: 1567年, 14世紀)。
  - 単位の換算や、年の計算をしない。引用の数値をそのまま使う。
  - 人物の動機・感情・因果関係を、引用になければ書かない。
・原文に書かれていないことは、一切足さない。「異説がある」「伝承とされる」など、原文がぼかしている点は、そのままぼかす。

【長さ】
・台本全体で350〜500文字。1文は60文字以内。小学生にも分かるやさしい言葉で。
・長さが足りないときは、別の話題を足さず、「軸に沿った具体的な描写(誰が、どこで、何を、どうした)」を、原文から丁寧に拾って増やす。

【構成の例(中身は真似しないこと。型だけの参考)】
  起(opinion): 「〇〇は、なぜ△△だったのでしょう？」
  承(fact): 「〇〇は、□□に生まれました。」「そして、□□で△△をしていました。」
  転(fact): 「ところが、△△の年に、◇◇が起きます。」「実は、◇◇は〜でした。」
  結(fact→opinion): 「こうして、〇〇は△△になりました。」「それは驚きですよね。あなたなら、どう思いますか？」

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
    """記事が、物語の軸をひとつに絞れない(概説・一覧のような)ときの合図。"""


def call_gemini_json(prompt):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=GEMINI_API_KEY,
                          http_options=types.HttpOptions(timeout=HTTP_TIMEOUT_SEC * 1000))  # ミリ秒
    last = None
    for model in GEMINI_MODELS:
        if out_of_time():
            break
        try:
            log(f"🤖 Gemini ({model}) で台本を作成中...")
            resp = client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            data = json.loads(resp.text.strip())
            if isinstance(data, dict):
                return data
        except Exception as e:
            last = e
            log(f"⚠️ {model}: {e}")
            # 429(無料枠の上限)のときは、少し待ってから次のモデルへ
            wait = 5 if ("429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)) else 2
            time.sleep(wait)
    raise RuntimeError(f"Gemini の呼び出しにすべて失敗しました: {last}")


def generate_draft(article, feedback=""):
    fb = ""
    if feedback:
        fb = ("【前回の台本の問題点(必ず直すこと)】\n" + feedback +
              "\n問題のあった文は、原文に根拠のある事実に差し替えるか、削除してください。\n\n")
    prompt = (SCRIPT_PROMPT
              .replace("{feedback}", fb)
              .replace("{title}", article["title"])
              .replace("{text}", article["text"]))
    d = call_gemini_json(prompt)
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


# ------------------------------------------------------------------ 3. 機械照合
def mechanical_check(sentences, source):
    """{文のindex: 理由} を返す。空なら全部OK。AIの自己申告ではなく、文字列の一致で確認する。"""
    src = norm(source)
    bad = {}
    for i, s in enumerate(sentences):
        text = (s.get("text") or "").strip()
        kind = s.get("type")
        if not text:
            bad[i] = "空の文"
        elif kind == "fact":
            q = s.get("evidence_quote") or ""
            nq = norm(q)
            if len(nq) < MIN_QUOTE_CHARS:
                bad[i] = "根拠の引用がない、または短すぎる"
            elif len(nq) > MAX_QUOTE_CHARS:
                bad[i] = "引用が長すぎる(該当部分だけを写すこと)"
            elif "..." in q or "…" in q:
                bad[i] = "引用に省略記号がある(連続した原文をそのまま写すこと)"
            elif nq not in src:
                bad[i] = "引用が原文に見つからない(一字一句そのまま写すこと)"
            else:
                missing = numbers(text) - numbers(q)
                if missing:
                    bad[i] = f"数字 {sorted(missing)} が引用に含まれていない"
                elif CAUSAL_JA.search(text) and not CAUSAL_EN.search(q):
                    bad[i] = (f"因果を表す言葉「{CAUSAL_JA.search(text).group(0)}」があるが、引用に因果の記述がない"
                              "(因果は、原文がそう述べているときだけ。なければ「そして」「その後」でつなぐ)")
        elif kind == "opinion":
            if numbers(text):
                bad[i] = "感想・問いかけの文に数字が含まれている(事実は fact にする)"
        else:
            bad[i] = f"type が不正: {kind!r}"
    return bad


# ------------------------------------------------------------------ 4. Groq の意味チェック
SEM_SYSTEM = """あなたは、歴史コンテンツの厳格なファクトチェッカーです。
入力は、台本の文のリストです。各要素: i(番号), kind("fact" か "opinion"), text(日本語の台本の文), quote(根拠として示された英語の原文の引用)。

判定ルール:
- kind="fact": quote が text の内容を「すべて」裏付けているときだけ ok=true。
  quote にない情報(人物の動機・感情、因果関係、場所、人数、数字、「最初の」「唯一の」などの強い断定)が text に含まれていれば ok=false。
  「〜とされる」「〜と言われる」など、quote がぼかしている内容を、text が断定に変えていても ok=false。
- kind="opinion": 新しい事実(出来事・数字・年代)を主張していなければ ok=true。主張していれば ok=false。
  他の fact の文に出てくる人物・物の名前を、問いかけや感想に使うのは問題ありません。
  ただし、台本のどこにも出てこない事実を、前提にしていれば ok=false。

次の JSON だけを返してください(説明文は不要):
{"results": [{"i": 0, "ok": true, "reason": "短い理由"}]}
"""


def call_groq_text(system, user):
    """Groq にチャットで問い合わせる。全モデル失敗なら None。"""
    if not GROQ_API_KEY:
        return None
    from groq import Groq
    client = Groq(api_key=GROQ_API_KEY, timeout=HTTP_TIMEOUT_SEC, max_retries=1)
    for model in GROQ_MODELS:
        if out_of_time():
            break
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            kw = {"reasoning_effort": "low"} if "gpt-oss" in model else {}
            try:
                resp = client.chat.completions.create(model=model, messages=msgs, temperature=0, **kw)
            except Exception as e:
                if not kw:
                    raise
                log(f"⚠️ Groq ({model}) reasoning_effort でエラー。外して再試行: {e}")
                resp = client.chat.completions.create(model=model, messages=msgs, temperature=0)
            return resp.choices[0].message.content
        except Exception as e:
            log(f"⚠️ Groq ({model}): {e}")
    return None


def list_groq_models():
    """このキーで使える Groq モデルの一覧(困ったときの確認用)。"""
    from groq import Groq
    return [m.id for m in Groq(api_key=GROQ_API_KEY).models.list().data]


def semantic_check(sentences, skip):
    """{文のindex: 理由} を返す。Groq が使えなければ None(=未実施)。"""
    items = [{"i": i, "kind": s.get("type"), "text": s.get("text"), "quote": s.get("evidence_quote")}
             for i, s in enumerate(sentences) if i not in skip]
    if not items:
        return {}
    log(f"🛡️ Groq で意味チェック中({len(items)}文)...")
    data = extract_json(call_groq_text(SEM_SYSTEM, json.dumps(items, ensure_ascii=False)))
    if not data or not isinstance(data.get("results"), list):
        return None
    fails, judged = {}, set()
    for r in data["results"]:
        try:
            idx = int(r["i"])
        except (KeyError, TypeError, ValueError):
            continue
        judged.add(idx)
        if r.get("ok") is not True:
            fails[idx] = r.get("reason") or "裏付けが不十分"
    for it in items:                       # 判定が返ってこなかった文は、安全側で失敗扱い
        if it["i"] not in judged:
            fails[it["i"]] = "AIが判定しなかった"
    return fails


# ------------------------------------------------------------------ 5. 検証つき生成
def script_text(sentences):
    return "".join(s["text"].strip() for s in sentences)


def build_feedback(sentences, problems):
    return "\n".join(f"・「{sentences[i].get('text', '')}」 → {why}" for i, why in sorted(problems.items()))


def check_payoff(draft, source):
    """最初の問いの「答え」が、台本の 転・結 の fact に入っているか。問題があれば理由を返す(OKなら None)。"""
    plan = draft.get("plan") or {}
    aq = norm(plan.get("answer_quote") or "")
    if len(aq) < MIN_QUOTE_CHARS:
        return "plan.answer_quote(問いの答えが書かれた、原文からの引用)がありません"
    if aq not in norm(source):
        return "plan.answer_quote が、原文に見つかりません(一字一句そのまま写すこと)"
    parts, sents = [], draft["sentences"]
    for i, s in enumerate(sents):
        q = norm(s.get("evidence_quote") or "")
        if s.get("type") == "fact" and q and (aq in q or q in aq):
            part = s.get("part")
            if part not in tuple(PARTS):              # part が付いていなければ、台本の後半かどうかで判定
                part = "転" if i >= len(sents) / 2 else "承"
            parts.append(part)
    if any(p in ("転", "結") for p in parts):        # 1つでも、転・結にあればOK
        return None
    if parts:
        return "問いの答えの文が、起・承にあります。転か結で語ってください"
    return "問いの答えとなる事実(answer_quote)が、台本の fact の文で使われていません"


def missing_parts(sentences):
    """起承転結のうち、台本に出てこない part を返す。"""
    present = {s.get("part") for s in sentences}
    return [p for p in PARTS if p not in present]


def estimate_seconds(text, speed=1.15):
    """VOICEVOX の読み上げ時間の、おおよその目安(秒)。実際は音声を作って測ること。"""
    n = len(text)
    return round(n / (6.5 * speed))


def generate_verified(article):
    """検証済みの台本データを返す。作れなければ None。"""
    feedback, draft, problems = "", None, {}
    sem_available = True
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if out_of_time():
            break
        log(f"✍️ 台本作成 {attempt}/{MAX_ATTEMPTS}")
        try:
            draft = generate_draft(article, feedback)
        except NarrativeUnsuitable as e:
            log(f"↪ 物語の軸を絞れない記事のため、見送ります: {e}")
            return None
        except Exception as e:
            log(f"⚠️ 台本生成に失敗: {e}")
            continue
        sents = draft["sentences"]
        mech = mechanical_check(sents, article["text"])
        sem = semantic_check(sents, skip=set(mech)) if sem_available else None
        if sem is None:
            sem_available = False
            sem = {}
        problems = {**sem, **mech}
        if not problems:
            draft["status"] = "verified" if sem_available else "mechanical_only"
            miss = missing_parts(sents)
            total = len(script_text(sents))
            soft = []
            if miss:
                soft.append(f"・起承転結のうち「{'・'.join(miss)}」の part の文がありません。4つすべてを使ってください。")
            if total < MIN_TARGET_CHARS:
                soft.append(f"・台本が短すぎます({total}字)。別の話題は足さず、軸に沿った具体的な描写を、原文から丁寧に拾って、"
                            f"{MIN_TARGET_CHARS}字以上にしてください(事実は、引用で裏付けたまま)。")
            pay = check_payoff(draft, article["text"])
            if pay:
                hook = (draft.get("plan") or {}).get("hook_question", "")
                soft.append(f"・最初の問い「{hook}」の答えが、台本にありません({pay})。原文から、答えが書かれた箇所を探して "
                            "plan.answer_quote に入れ、その内容を、転か結の fact の文にしてください。"
                            "原文に答えがなければ、問いを「何が起きたか」などに変えるか、narrative_ok を false にしてください。")
            if pay and attempt >= MAX_ATTEMPTS:
                log(f"✖ 問いに対する答えが入った台本を作れなかったため、この記事は見送ります: {pay}")
                return None
            if soft and attempt < MAX_ATTEMPTS:        # 構成・長さの不足は、事実の誤りではないので、再生成のみ
                log(f"   ↪ 構成/長さを直すため、再生成します(起承転結の不足: {''.join(miss) or 'なし'} / {total}字)")
                feedback = "\n".join(soft)
                continue
            draft["missing_parts"] = miss
            return draft
        log(f"   ✖ {len(problems)}文に問題あり")
        for i, why in sorted(problems.items()):
            log(f"     - {sents[i].get('text', '')[:30]}… : {why}")
        feedback = build_feedback(sents, problems)

    if not draft:
        return None
    # 再生成しても残った問題の文は、削除する
    kept = [s for i, s in enumerate(draft["sentences"]) if i not in problems]
    facts = [s for s in kept if s.get("type") == "fact"]
    if len(facts) < MIN_FACT_SENTENCES or len(script_text(kept)) < MIN_SCRIPT_CHARS:
        log("✖ 問題のある文を除くと、台本として成立しないため、この記事は見送ります。")
        return None
    draft["sentences"] = kept
    pay = check_payoff(draft, article["text"])
    if pay:
        log(f"✖ 問題のある文を除いたら、問いの答えがなくなったため、この記事は見送ります: {pay}")
        return None
    draft["status"] = "partial" if sem_available else "partial_mechanical_only"
    draft["dropped_count"] = len(problems)
    return draft


# ------------------------------------------------------------------ 6. Discord 投稿
STATUS_LABEL = {
    "verified": "✅ 引用の機械照合 + AI意味チェック 合格",
    "mechanical_only": "🟡 引用の機械照合のみ合格(AI意味チェックは未実施)。人の確認を必ず",
    "partial": "🟠 問題のある文を削除済み(照合・AIチェックは残りが合格)。人の確認を必ず",
    "partial_mechanical_only": "🟠 問題のある文を削除済み(機械照合のみ)。人の確認を必ず",
}


def chunk_lines(lines, limit=1900):
    chunks, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += ln + "\n"
    if cur:
        chunks.append(cur)
    return chunks


def build_messages(article, data):
    jst = timezone(timedelta(hours=9))
    accessed = datetime.now(jst).strftime("%Y/%m/%d %H:%M")
    sents = data["sentences"]
    main = (f"**【概要】**\n{data['summary']}\n\n"
            f"**【台本（VOICEVOX）】**\n{script_text(sents)}\n\n"
            f"📚 **【出典情報】**\n"
            f"・原題: `{article['title']}`\n"
            f"・URL: <{article['fullurl']}>\n"
            f"・版(revid): {article.get('revid')}\n"
            f"・ライセンス: CC BY-SA 4.0\n"
            f"・閲覧日時: {accessed} (JST)\n"
            f"・検証状態: {STATUS_LABEL.get(data['status'], data['status'])}")
    if data.get("dropped_count"):
        main += f"\n・削除した文: {data['dropped_count']}文"
    plan = data.get("plan") or {}
    if plan.get("axis"):
        main += f"\n・物語の軸: {plan['axis']}\n・問い→答え: {plan.get('hook_question', '')} → {plan.get('payoff', '')}"
        if plan.get("answer_quote"):
            main += f"\n・答えの根拠: \"{plan['answer_quote'][:200]}\""
    text = script_text(sents)
    n_op = sum(1 for s in sents if s.get("type") == "opinion")
    main += f"\n・長さ: {len(text)}字(読み上げ 約{estimate_seconds(text)}秒の目安) / 感想・問いかけ {n_op}文"
    if data.get("missing_parts"):
        main += f"\n・⚠️ 起承転結のうち不足: {''.join(data['missing_parts'])}"
    main = main[:1990]

    ev_lines = ["🔎 **【根拠一覧】(fact の文と、原文の該当箇所)**"]
    n = 0
    for s in sents:
        if s.get("type") == "fact":
            n += 1
            ev_lines.append(f"{n}. [{s.get('part', '?')}] {s['text']}\n   └ \"{s['evidence_quote']}\"")
    ev_lines.append("\n※ 感想・問いかけの文(根拠なし)は、台本の冒頭か最後のまとめだけのはず。")
    evidence = chunk_lines(ev_lines)

    imgs = "\n".join(f"{i + 1}. `{p}`" for i, p in enumerate(data.get("image_prompts", [])))
    vids = "\n".join(f"{i + 1}. `{p}`" for i, p in enumerate(data.get("video_prompts", [])))
    prompts = (f"🎨 **【画像プロンプト】**\n{imgs}\n\n🎬 **【動画プロンプト】**\n{vids}")[:1990]
    return main, evidence, prompts


def save_trivia_json(article, data, thread_id=None):
    """後続の音声合成・動画合成のため、台本などを JSON で保存する。
    投稿前に thread_id=None で保存し、投稿後に thread_id を入れて上書きする。"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    sents = data["sentences"]
    out = {
        "thread_id": str(thread_id) if thread_id else None,   # 桁が大きいので文字列で持つ
        "title": data.get("title", article["title"]),
        "summary": data.get("summary", ""),
        "script_text": script_text(sents),
        "sentences": sents,
        "image_prompts": data.get("image_prompts", []),
        "video_prompts": data.get("video_prompts", []),
        "status": data.get("status"),
        "plan": data.get("plan"),
        "article": {"title": article["title"], "url": article.get("fullurl"),
                    "revid": article.get("revid"), "license": "CC BY-SA 4.0"},
        "article_url": article.get("fullurl"),
    }
    path = os.path.join(OUTPUT_DIR, "trivia_result.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    log(f"💾 JSON を保存しました: {path} (thread_id={out['thread_id']})")
    return path


def post_to_discord(article, data):
    """Discord に投稿する。成功したら True。"""
    import discord

    main_msg, evidence, prompts = build_messages(article, data)
    result = {"ok": False}

    class Poster(discord.Client):
        async def on_ready(self):
            log(f"🤖 Discord Bot ログイン完了: {self.user}")
            try:
                ch = self.get_channel(FORUM_CHANNEL_ID) or await self.fetch_channel(FORUM_CHANNEL_ID)
                if not isinstance(ch, discord.ForumChannel):
                    log("❌ フォーラムチャンネルが見つかりませんでした。")
                else:
                    t = await ch.create_thread(name=f"📜 {data['title']}"[:100], content=main_msg)
                    thread = t.thread
                    save_trivia_json(article, data, thread.id)     # まず thread_id を保存(以降の投稿が失敗しても残す)
                    for c in evidence:
                        await thread.send(content=c)
                    await thread.send(content=prompts)
                    log(f"🎉 投稿完了: {thread.name} (Thread ID: {thread.id})")
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
    """成功なら 0、投稿できなかったら 1 で終了する(Actions の後続ステップを止めるため)。"""
    if not GEMINI_API_KEY:
        log("❌ GEMINI_API_KEY が未設定です。")
        return 1
    if not DISCORD_TOKEN or not FORUM_CHANNEL_ID:
        log("❌ DISCORD_TOKEN / FORUM_CHANNEL_ID が未設定です。")
        return 1
    if not GROQ_API_KEY:
        log("⚠️ GROQ_API_KEY が未設定です。AI意味チェックなし(機械照合のみ)で進めます。")

    for n in range(1, MAX_ARTICLES + 1):
        if out_of_time():
            break
        log(f"\n===== 記事選び {n}/{MAX_ARTICLES} =====")
        article = pick_article()
        if not article:
            log("❌ 条件に合う記事が見つかりませんでした。")
            continue
        log(f"📖 {article['title']}  {article['fullurl']}  ({len(article['text'])}字"
            f"{'、長いため先頭のみ使用' if article.get('truncated') else ''})")
        data = generate_verified(article)
        if data:
            save_trivia_json(article, data, None)       # 投稿前にも保存(thread_id は後で追記)
            log("🚀 Discord へ投稿します...")
            if post_to_discord(article, data):
                return 0
            log("❌ Discord への投稿に失敗しました。")
            return 1
        log("↪ 別の記事で、やり直します。")
    log("❌ 検証を通る台本を作れなかったため、今回は投稿しません。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
