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


GEMINI_MODELS = _env_list("GEMINI_MODELS", "gemini-3.8-flash,gemini-2.0-flash")
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
MAX_SOURCE_CHARS = 12000                                  # LLM に渡す原文の上限
MIN_ARTICLE_CHARS = int(os.environ.get("MIN_ARTICLE_CHARS", "3000"))  # 短い記事は材料不足として捨てる
MIN_QUOTE_CHARS = 15
MAX_QUOTE_CHARS = 300
MIN_SCRIPT_CHARS = 150                                    # 検証後の台本がこれより短ければ捨てる
MIN_FACT_SENTENCES = 3
PARTS = "起承転結"

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
下の【原文】だけを材料に、YouTube Shorts 用のナレーション台本を書いてください。

【最重要ルール】
・台本は「1文ずつ」に分け、各文に type を付けます。
  - "fact" : 原文に書かれた事実を述べる文。必ず evidence_quote を付ける。
  - "opinion": 問いかけ・感想・まとめの文。事実を新しく主張してはいけない(数字・固有名詞・年代を入れない)。
    「〜かもしれません」「〜と思いませんか？」のようにぼかす。evidence_quote は null。
・fact の evidence_quote は、【原文】から一字一句そのまま写した英語の連続した引用(15〜300文字)。
  要約・言い換え・「...」での省略・翻訳をしてはいけません。
・fact の text に入れてよい内容は、その引用が直接裏付けるものだけです。
  - 数字・人名・地名・年代は、引用にあるものだけを使う。アラビア数字で書く(例: 1567年, 14世紀)。
  - 単位の換算や、年の計算をしない。引用の数値をそのまま使う。
  - 人物の動機・感情・因果関係を、引用になければ書かない。
・原文に書かれていないことは、一切足さない。材料が少なければ短くてかまいません(無理に伸ばさない)。
・長さ: 原文に材料が十分あるときは、全体で400〜550文字(VOICEVOXで約1分)。fact は6文以上。
  1文は60文字以内。小学生にも分かるやさしい言葉で。
・構成は「起承転結」。各文に part("起","承","転","結" のどれか)を付け、4つすべてを使います。
  - 起: 興味を引く問いかけ、または印象的な事実で始める。
  - 承: 背景や状況を、fact で説明する。
  - 転: 意外な展開・対比・ピンチ・状況の変化を、fact で述べる(原文にある出来事だけ)。
  - 結: 結末の fact を述べたあと、感想や問いかけでまとめる。
・opinion(感想・問いかけ)は全体で2〜4文。次のどちらかの形にします。
  (a) 直前の fact への素直な反応(例:「それは驚きですよね。」)
  (b) 視聴者への問いかけ(例:「あなたなら、どう思いますか？」)
  新しい事実は入れません。台本を長くするための水増しにも使いません。
・「異説がある」「伝承とされる」など、原文がぼかしている点は、台本でもそのままぼかす。

{feedback}
【記事タイトル】{title}
【原文】
{text}

次の JSON だけを返してください:
{{
  "title": "惹きつける日本語タイトル(20文字以内)",
  "summary": "原文の事実だけによる概要(120文字程度)",
  "sentences": [
    {{"text": "日本語の1文", "part": "起", "type": "opinion", "evidence_quote": null}},
    {{"text": "日本語の1文", "part": "承", "type": "fact", "evidence_quote": "原文からの英語の引用"}}
  ],
  "image_prompts": ["英語の画像プロンプト4つ(歴史画風、文字なし、残酷な描写なし)"],
  "video_prompts": ["英語のカメラワーク指示4つ"]
}}
"""


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
            time.sleep(2)
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
- kind="opinion": 新しい事実(出来事・数字・固有名詞・年代)を主張していなければ ok=true。主張していれば ok=false。

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
            if miss and attempt < MAX_ATTEMPTS:        # 構成の不足は、事実の誤りではないので、再生成のみ
                log(f"   ↪ 起承転結のうち {''.join(miss)} がないため、再生成します")
                feedback = (f"・起承転結のうち「{'・'.join(miss)}」の part の文がありません。"
                            "4つすべてを使い、事実の文は原文の引用で裏付けたまま、構成を直してください。")
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
        log(f"📖 {article['title']}  {article['fullurl']}  ({len(article['text'])}字)")
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
