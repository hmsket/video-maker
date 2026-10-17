// Cloudflare Worker — video-maker / Discord Interaction Handler
// 機能:
//   - Ed25519 署名検証
//   - /fact : Groq でファクトチェック → 不合格なら AI が自動修正 → 再チェック（最大 MAX_AUTO_FIX_ROUNDS 回）
//             通過したら「🔊 音声・動画を作成する」ボタン付きで確定文面を投稿
//             完全に通らない場合は「🔁 再チェック」ボタン付きで最終台本を提示
//   - btn_fetch_trivia : fetch_trivia.yml を起動
//   - btn_generate_video / btn_genarate_video : generate_video.yml を起動
//   - btn_fact_recheck : 最新の台本をスタンプ不問で再チェック（AI自動修正ループも走る）
//
// 注意: この内容を Cloudflare ダッシュボードの Worker エディタ（または wrangler）へ
//       全体を置き換えて反映してください。

const GROQ_MODEL = "openai/gpt-oss-120b";
const MAX_AUTO_FIX_ROUNDS = 2;   // AI自動修正 → 再チェック の最大回数（合計チェック回数は 1 + この値）
const META_MARKER = "【生成メタデータ】";
const MIN_SCRIPT_CHARS = 120;    // AI修正結果の最短文字数（壊れた短文を取り込まない保険）
const SCRIPT_MAX_CHARS = 340;    // 台本の目標上限（リポジトリ側のルールと同じ基準）
const DEBUG = false;             // true にすると対象台本などの詳細ログが出る

// Ed25519 署名検証用の軽量ピュアJS実装
class Ed25519 {
  static hexToBytes(hex) {
    const bytes = new Uint8Array(hex.length / 2);
    for (let i = 0; i < hex.length; i += 2) {
      bytes[i / 2] = parseInt(hex.substring(i, i + 2), 16);
    }
    return bytes;
  }

  static async verify(signatureHex, timestamp, bodyText, publicKeyHex) {
    try {
      const signature = this.hexToBytes(signatureHex);
      const publicKeyBytes = this.hexToBytes(publicKeyHex);
      const encoder = new TextEncoder();
      const message = encoder.encode(timestamp + bodyText);

      const key = await crypto.subtle.importKey(
        'raw',
        publicKeyBytes,
        { name: 'Ed25519' },
        false,
        ['verify']
      );

      return await crypto.subtle.verify(
        'Ed25519',
        key,
        signature,
        message
      );
    } catch (e) {
      console.error('Verify Exception:', e);
      return false;
    }
  }
}

async function triggerGitHubWorkflow(env, workflowFile = 'generate_video.yml', payload = {}) {
  const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/${workflowFile}/dispatches`;

  const requestBody = {
    ref: 'main',
  };

  if (payload && payload.thread_id) {
    requestBody.inputs = {
      thread_id: String(payload.thread_id)
    };
  }

  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${env.GITHUB_PAT}`,
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'Cloudflare-Worker-Discord-Bot',
      },
      body: JSON.stringify(requestBody),
    });

    if (!response.ok) {
      const errorText = await response.text();
      console.error(`[ERROR] GitHub Actions Trigger Failed (${response.status}):`, errorText);
    } else {
      console.log(`[SUCCESS] GitHub Actions (${workflowFile}) triggered!`);
    }
  } catch (e) {
    console.error('triggerGitHubWorkflow Exception:', e);
  }
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405 });

    const signature = request.headers.get('x-signature-ed25519');
    const timestamp = request.headers.get('x-signature-timestamp');
    const bodyText = await request.text();

    if (!signature || !timestamp) return new Response('Bad Request Signature', { status: 401 });

    const isVerified = await Ed25519.verify(signature, timestamp, bodyText, env.DISCORD_PUBLIC_KEY);
    if (!isVerified) return new Response('Invalid signature', { status: 401 });

    const interaction = JSON.parse(bodyText);

    // 1. PING応答
    if (interaction.type === 1) {
      return new Response(JSON.stringify({ type: 1 }), {
        headers: { 'Content-Type': 'application/json' },
      });
    }

    const channelId = interaction.channel_id || (interaction.channel && interaction.channel.id);

    // 2. スラッシュコマンド実行 (/fact)
    if (interaction.type === 2) {
      if (interaction.data.name === 'fact') {
        ctx.waitUntil(runGroqFactCheck(channelId, env, { source: 'command' }));
        return jsonResponse({
          type: 4,
          data: { content: '🛡️ ファクトチェックを実行中... 不合格ならAIの自動修正も試みます（最大2回）。', flags: 64 }
        });
      }
    }

    // 3. ボタン押下 (Component Interaction)
    if (interaction.type === 3) {
      const customId = interaction.data.custom_id;

      // ネタ作成（取得）ボタン
      if (customId === 'btn_fetch_trivia') {
        ctx.waitUntil(triggerGitHubWorkflow(env, 'fetch_trivia.yml'));
        return jsonResponse({
          type: 4,
          data: { content: '🔍 新しいネタを取得・作成中... Discordフォーラムを確認してください！', flags: 64 }
        });
      }

      // 音声・動画作成ボタン (generate_video.yml を起動)
      if (customId === 'btn_generate_video' || customId === 'btn_genarate_video') {
        ctx.waitUntil(triggerGitHubWorkflow(env, 'generate_video.yml', { thread_id: channelId }));
        return jsonResponse({
          type: 4,
          data: { content: '🚀 動画作成ワークフロー (generate_video.yml) を起動しました！完了までお待ちください。', flags: 64 }
        });
      }

      // 再ファクトチェックボタン（スタンプ不問で最新台本を対象に、AI自動修正ループも実行）
      if (customId === 'btn_fact_recheck') {
        ctx.waitUntil(runGroqFactCheck(channelId, env, { source: 'button' }));
        return jsonResponse({
          type: 4,
          data: { content: '🛡️ 最新の台本を再チェック中...', flags: 64 }
        });
      }
    }

    return new Response('Unknown interaction', { status: 400 });
  }
};

function jsonResponse(data) {
  return new Response(JSON.stringify(data), {
    headers: { 'Content-Type': 'application/json' }
  });
}

// ---------------------------------------------------------------- 台本・メタデータ抽出

function parseScriptText(content) {
  if (!content) return "";
  const match = content.match(/```(?:\w+)?\s*\n?([\s\S]*?)```/);
  if (match && match[1]) {
    return match[1].trim();
  }
  // コードブロック無しのメッセージ（元台本のプレーン投稿等）は本文をそのまま使う
  return content.trim();
}

// 【生成メタデータ】はパイプライン内部のJSONで台本ではない
function isMetaMessage(content) {
  return typeof content === 'string' && content.includes(META_MARKER);
}

// /fact 用: スタンプの付いたメッセージ（ Bot メッセージも許容＝元台本が Bot 投稿のため）
function pickScriptByStamp(messages) {
  for (const msg of messages) {
    const content = msg.content || "";
    if (isMetaMessage(content)) continue;
    const hasReaction = Array.isArray(msg.reactions) && msg.reactions.some(r => r.count > 0);
    if (!hasReaction) continue;
    const text = parseScriptText(content);
    if (text.length > 0) return text;
  }
  return "";
}

// /fact のフォールバック: スタンプが無ければ最新のユーザー投稿
function pickLatestUserScript(messages) {
  for (const msg of messages) {
    if (msg.author && msg.author.bot === true) continue;
    const text = parseScriptText(msg.content || "");
    if (text.length > 0) return text;
  }
  return "";
}

// 再チェックボタン用: スタンプ不問で「最新の台本っぽいコードブロック」を探す
function pickLatestScriptAny(messages) {
  for (const msg of messages) {  // Discord API は新しい順に返す
    const content = msg.content || "";
    if (isMetaMessage(content)) continue;
    const text = parseScriptText(content);
    if (text.length > 0 && !text.includes('"video_maker_meta"')) return text;
  }
  return "";
}

// メタデータ(JSON)から出典記事情報を取り出す → Wikipedia 本文の取得に使う
function findMetaArticle(messages) {
  for (const msg of messages) {
    const content = msg.content || "";
    if (!isMetaMessage(content)) continue;
    const match = content.match(/```(?:json)?\s*\n?([\s\S]*?)```/);
    if (!match) continue;
    try {
      const meta = JSON.parse(match[1].trim());
      if (meta && meta.article) return meta.article;
    } catch (e) {
      console.error('findMetaArticle: JSON parse failed');
    }
  }
  return null;
}

// Wikipedia の完全な原文を取得（AIの自動修正が事実を捏造しないよう、根拠として与える）
// 取得は revid を優先し、無い場合は article.title にフォールバックする
async function fetchWikiArticleText(article) {
  if (!article) return "";
  const params = new URLSearchParams({
    format: 'json',
    formatversion: '2',
    action: 'query',
    prop: 'extracts',
    explaintext: '1',
  });
  if (article.revid) {
    params.set('revids', String(article.revid));
  } else if (article.title) {
    params.set('titles', article.title);
  } else {
    return "";
  }
  try {
    const res = await fetch(`https://en.wikipedia.org/w/api.php?${params.toString()}`, {
      headers: { 'User-Agent': 'VideoMakerBot/1.1 (Cloudflare Worker)' },
    });
    if (!res.ok) {
      console.error(`fetchWikiArticleText: HTTP ${res.status}`);
      return "";
    }
    const data = await res.json();
    const extract = data?.query?.pages?.[0]?.extract || "";
    return extract.length > 8000 ? extract.slice(0, 8000) : extract;
  } catch (e) {
    console.error('fetchWikiArticleText exception:', e);
    return "";
  }
}

async function fetchMessages(channelId, env) {
  const res = await fetch(`https://discord.com/api/v10/channels/${channelId}/messages?limit=25`, {
    headers: { Authorization: `Bot ${env.DISCORD_TOKEN}` },
  });
  if (!res.ok) {
    console.error(`[ERROR] Messages fetch failed (${res.status}): ${await res.text()}`);
    return null;
  }
  const data = await res.json();
  return Array.isArray(data) ? data : null;
}

// ---------------------------------------------------------------- ファクトチェック & AI自動修正ループ

async function runGroqFactCheck(channelId, env, options = {}) {
  try {
    const messages = await fetchMessages(channelId, env);
    if (!messages) {
      await postToThread(channelId, env, "⚠️ メッセージ一覧の取得に失敗しました。");
      return;
    }

    // チェック対象の台本を決める
    let targetText = "";
    if (options.source === 'button') {
      // 再チェックボタン: スタンプ不問で最新の台本を拾う（ 手動修正返信 / AI修正版 / 確定文面のどこでも ）
      targetText = pickLatestScriptAny(messages);
      if (!targetText) targetText = pickScriptByStamp(messages);
    } else {
      // /fact コマンド: 従来どおりスタンプ付きメッセージを優先
      targetText = pickScriptByStamp(messages);
      if (!targetText) targetText = pickLatestUserScript(messages);
    }

    if (!targetText) {
      await postToThread(channelId, env,
        "⚠️ 対象メッセージが見つかりませんでした。チェックしたいメッセージにスタンプを付けてから `/fact` を実行してください。");
      return;
    }

    if (DEBUG) console.log(`[FACT] target=${targetText.length} chars (source=${options.source})`);

    // AI修正の根拠にする出典を事前に取得（スレッドにメタデータが無い場合は "" で続行）
    const wikiText = await fetchWikiArticleText(findMetaArticle(messages));

    // ── チェック → (不合格なら) AI自動修正 → 再チェック のループ ──
    let script = targetText;
    const maxChecks = 1 + MAX_AUTO_FIX_ROUNDS;

    for (let round = 1; round <= maxChecks; round++) {
      const check = await checkWithGroq(script, env.GROQ_API_KEY, wikiText);

      if (check.ok) {
        const note = round > 1 ? `（🛠️ AI自動修正 ${round - 1} 回目で通過）` : "";
        await postCheckedMessage(channelId, env, script, note);
        return;
      }

      // 不合格内容は人間にも見えるように投稿（従来どおり）
      const isLastRound = round === maxChecks;
      await postToThread(
        channelId,
        env,
        `❌ **ファクトチェック不合格** (${round}/${maxChecks})\n${check.reason}\n\n` +
        (isLastRound
          ? "🛠️ AI自動修正が上限に達しました。下の台本を手で直してスタンプを付けて `/fact` を実行するか、🔁 ボタンでそのまま再チェックできます。"
          : "🛠️ AIが指摘を踏まえて台本を修正し、再チェックします...")
      );

      if (isLastRound) break;

      const revised = await autoFixScript(script, check.reason, wikiText, env.GROQ_API_KEY);
      if (!revised) {
        await postToThread(channelId, env,
          "⚠️ AI自動修正に失敗しました。手動で修正し、スタンプを付けて `/fact` を実行してください。");
        return;
      }

      // 修正版を投稿。prepare_trivia.py は「スレッド内の最新コードブロック」を台本として採用するため、
      // ここで投稿しておけば、この台本がそのまま動画生成に使われる。
      await postToThread(
        channelId,
        env,
        `🧪 **AI自動修正版 (${round}/${MAX_AUTO_FIX_ROUNDS})** 違和感があれば、この台本をコードブロックで編集して返信投稿してください。\n\`\`\`\n${revised}\n\`\`\``
      );
      script = revised;
    }

    // 全ラウンドで不合格だった場合 → 最終台本 + 再チェックボタンを提示
    await postRecheckRequest(channelId, env, script);
  } catch (err) {
    console.error("runGroqFactCheck fatal error:", err);
    await postToThread(channelId, env, `⚠️ ファクトチェック処理中に例外が発生しました:\n\`\`\`${err.message}\`\`\``);
  }
}

// ✅ 通過時の投稿（従来と同じく 🔊 ボタン付き）
function postCheckedMessage(channelId, env, script, note = "") {
  return fetch(`https://discord.com/api/v10/channels/${channelId}/messages`, {
    method: 'POST',
    headers: {
      Authorization: `Bot ${env.DISCORD_TOKEN}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      content: `✅ **ファクトチェック通過！**${note}\n\n確定文面:\n\`\`\`\n${script}\n\`\`\``,
      components: [{
        type: 1,
        components: [{
          type: 2,
          style: 3,
          label: "🔊 音声・動画を作成する",
          custom_id: "btn_genarate_video"
        }]
      }]
    })
  });
}

// 🔁 全ラウンド失敗時の投稿（再チェックボタン付き）
function postRecheckRequest(channelId, env, script) {
  return fetch(`https://discord.com/api/v10/channels/${channelId}/messages`, {
    method: 'POST',
    headers: {
      Authorization: `Bot ${env.DISCORD_TOKEN}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      content:
        `🔁 **AI自動修正の上限に達しました。**\n\n` +
        `現時点の最終台本:\n\`\`\`\n${script}\n\`\`\`\n\n` +
        `必要なら手で直して再度 \`/fact\`、または下のボタンでそのまま再チェックできます。`,
      components: [{
        type: 1,
        components: [{
          type: 2,
          style: 1,
          label: "🛡️ 再ファクトチェック",
          custom_id: "btn_fact_recheck"
        }]
      }]
    })
  });
}

// 🛠️ AI修正: 指摘を解消する修正版台本を生成する
// 方針: 「安全側に倒して全部ぼかす」ではなく、原文に裏付けのある生きた表現は保持する
const AUTO_FIX_SYSTEM_PROMPT = `あなたはYouTube Shorts向けの歴史雑学台本のファクトチェック指摘を解消する台本作家です。
最重要目標は「ファクトチェックに通ること」と、忘れてはならない「おもしろい物語のままにしておくこと」です。
指摘された部分だけを書き換え、完成版台本を1つだけ出力してください。

【最重要の方針（必ず守る）】
- 元の台本の面白さ・テンポ・問いかけ・感情描写・具体的数字は、指摘と抵触する部分を除き**そのまま保持する**。
- 文面を整えるための全面改稿はしない。「とされています」「言われます」で全体を包む没個性的な文に書き換えることは禁止。
- 【資料】に裏付けがある情報（人数・年号・固有名詞・出来事・場面描写）は、ぼかしを追加せず生のまま使ってよい（むしろ使うべき）。
- 直すのは次の2種類だけ:
  1. 【資料】と矛盾・あるいは【資料】に根拠がないのに断定している部分 → 資料の内容に合わせるか、最小限のぼかしにする
  2. ファクトチェックの指摘が具体的に指している文 → その文だけ直す
- 資料が「（なし）」の場合は、明らかな歴史的誤実のみを最小限修正し、それ以外は元の台本をほぼそのまま出力する。

【形式ルール】
- 「です・ます調」の親しみやすいトーンを維持する。
- 全体は300〜340文字を維持する。1文は35文字以内。起承転結の構成は崩さない。
- 新しい事実・数字を捏造しない（【資料】内の情報の範囲でのみ修正）。
- 出力は台本本文のみ。前置き・後書き・引用符・コードブロックは付けない。`;

async function autoFixScript(scriptText, feedback, wikiText, apiKey) {
  const userPrompt = [
    "【修正対象の台本】",
    scriptText,
    "",
    "【ファクトチェックの指摘】",
    feedback,
    "",
    "【参考資料 (Wikipedia 記事からの抜粋)】",
    wikiText || "（なし）",
    "",
    "修正後の台本文のみを出力してください。",
  ].join("\n");

  try {
    const res = await fetch('https://api.groq.com/openai/v1/chat/completions', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${apiKey}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        model: GROQ_MODEL,
        messages: [
          { role: 'system', content: AUTO_FIX_SYSTEM_PROMPT },
          { role: 'user', content: userPrompt }
        ],
        temperature: 0.2
      })
    });

    if (!res.ok) {
      console.error(`autoFixScript: Groq HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
      return null;
    }

    const data = await res.json();
    let text = (data?.choices?.[0]?.message?.content || "").trim();
    if (!text) {
      console.error('autoFixScript: 空の応答');
      return null;
    }

    // 念のため前後のコードブロックや前置きを剥ぐ
    text = text.replace(/^```[a-z]*\s*\n?/i, "").replace(/\n?```\s*$/i, "").trim();
    text = text.replace(/^(修正後の台本|台本)[:：]\s*/i, "").trim();

    // 台本として明らかに破綻している出力は取り込まない保険
    if (text.length < MIN_SCRIPT_CHARS || text.length > SCRIPT_MAX_CHARS + 200) {
      console.error(`autoFixScript: 出力の長さが不自然です (${text.length}文字)`);
      return null;
    }
    return text;
  } catch (e) {
    console.error("autoFixScript exception:", e);
    return null;
  }
}

// 🛡️ Groq によるファクトチェック
// wikiText がある場合: 「その Wikipedia 原文に裏付けがあるか」を判定基準にする
// wikiText がない場合: 明らかな歴史的誤実だけを不合格にする（遠慮がちな書き換え要求をしない）
async function checkWithGroq(text, apiKey, wikiText = "") {
  const sourceRule = wikiText
    ? `【判定の手順（重要）】
- この台本の下に【参考文献】 (Wikipedia 原文) が付いている。**判定はこの文献に基づくこと**。
- 文献に書かれている出来事・人数・年号・固有名詞・場面描写は「事実」として扱い、これらを理由に不合格にしたり、「記録がない」「詳細は不明」などと指摘してはならない。
- 不合格にするのは、文献と矛盾する内容、および文献に記載が無い事実の断定だけ。
- 文献のある台本に「とされています」のようなぼかしを追加するよう要求してはならない（生き生きした表現は台本の価値）。`
    : `【判定の手順（重要）】
- 参考文献が提供されていない場合は**非常に寛容に判定する**。明らかに確立した歴史的事実と真っ向から矛盾する断定だけを不合格にする。
- 細部の数字・描写に「根拠が不明」といった指摘をしない（資料は提供されていないため、確認できないこと自体は許容する）。
- 「とされています」に書き換える退屈な修正を要求してはならない。`;

  const systemPrompt = `あなたはYouTube Shorts等の歴史雑学動画のファクトチェック担当者です。
動画台本として「エンタメ性と史実のバランス」を判定してください。台本の面白さは重要な価値であり、あなたの仕事は「明らかな誤実の検出」です。

${sourceRule}

【合格（ok: true）とする基準】
- あなたが参照した資料（または既知の明確な歴史的事実）と矛盾する内容が無いこと。
- 通説や説のひとつとして存在していれば、ある程度の強調表現やキャッチーな言い回しは許容する。
- 「〜と伝えられている」「〜という説もある」といったボカし表現で成立している場合は合格とする。

【不合格（ok: false）とする基準】
- 資料（または既知の明確な歴史的事実）と明確に矛盾する内容がある場合。
- 現代の歴史学で完全に否定されている都市伝説を「絶対の史実」として断定している場合。
- 根拠のない数字や事実を「捏造」と断定するのは、参照した資料がある場合に限る。

回答は必ず以下のJSONフォーマットのみで返してください：
{"ok": true, "reason": ""}
または
{"ok": false, "reason": "不合格の具体的な理由と、どう修正すれば通るかの簡単なアドバイス"}`;

  const userPrompt = wikiText
    ? `【検証対象の台本】\n${text}\n\n【参考文献 (Wikipedia 記事からの抜粋)】\n${wikiText}`
    : `【検証対象の台本】\n${text}`;

  try {
    const res = await fetch('https://api.groq.com/openai/v1/chat/completions', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${apiKey}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        model: GROQ_MODEL,
        messages: [
          { role: 'system', content: systemPrompt },
          { role: 'user', content: userPrompt }
        ],
        temperature: 0.2,
        response_format: { type: "json_object" }
      })
    });

    const data = await res.json();

    if (!res.ok) {
      console.error('Groq API Error Response:', JSON.stringify(data));
      return { ok: false, reason: `Groq API エラー (${res.status}): ${data.error?.message || '不明なエラー'}` };
    }

    if (!data.choices || !data.choices[0] || !data.choices[0].message) {
      console.error('Groq Unexpected Response Structure:', JSON.stringify(data));
      return { ok: false, reason: 'Groq API からの応答フォーマットが不正です。' };
    }

    const reply = data.choices[0].message.content;
    const parsed = JSON.parse(reply);
    return {
      ok: Boolean(parsed.ok),
      reason: parsed.reason || ""
    };

  } catch (e) {
    console.error("checkWithGroq Exception:", e);
    return { ok: false, reason: `Groqチェック処理例外: ${e.message}` };
  }
}

async function postToThread(channelId, env, content) {
  if (!channelId) {
    console.error("postToThread: channelId is missing or undefined!");
    return;
  }

  try {
    const res = await fetch(`https://discord.com/api/v10/channels/${channelId}/messages`, {
      method: 'POST',
      headers: {
        Authorization: `Bot ${env.DISCORD_TOKEN}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ content })
    });

    if (!res.ok) {
      const errText = await res.text();
      console.error(`postToThread failed (${res.status}): ${errText}`);
    }
  } catch (e) {
    console.error("postToThread exception:", e);
  }
}
