// Cloudflare Worker — video-maker / Discord Interaction Handler
// 機能:
//   - Ed25519 署名検証
//   - /fact : Groq でファクトチェック → 不合格なら AI が自動修正 → 再チェック
//             （自動修正は最大 MAX_AUTO_FIX_ROUNDS 回）
//             通過したら「🔊 音声・動画を作成する」ボタン付きで確定文面を投稿
//             完全に通らない場合は「🔁 再チェック」ボタン付きで最終台本を提示
//   - btn_fetch_trivia : fetch_trivia.yml を起動
//   - btn_generate_video / btn_genarate_video : generate_video.yml を起動
//   - btn_fact_recheck : 最新の台本をスタンプ不問で再チェック（AI自動修正ループも実行）
//
// ▶ 実行方式（重要）: Cloudflare Workers は「応答を返した後、waitUntil は最長30秒」
//   で打ち切られる。チェック→修正→再チェックを1回の呼び出し内で待機しながら行うと
//   30秒で捨てられるため、各ステップ（チェック/修正）を「新しいWorker呼び出し」で実行し、
//   自分自身の公開URLへ HTTP POST して次段を起動するカスケード方式にしている。
//   ※ 環境変数 INTERNAL_SECRET（任意のランダム文字列）を必ず設定すること。
//     これが無いと内部カスケードが拒否され、最初のチェックしか動かない。

const GROQ_MODEL = "openai/gpt-oss-120b";
const MAX_AUTO_FIX_ROUNDS = 2;   // AI自動修正 → 再チェック の最大回数（チェック合計は 1 + この値）
const META_MARKER = "【生成メタデータ】";
const MIN_SCRIPT_CHARS = 120;    // AI修正結果の最短文字数（壊れた短文を取り込まない保険）
const SCRIPT_MAX_CHARS = 1500;    // 台本の目標上限（リポジトリ側のルールと同じ基準）
const WIKI_TEXT_MAX_CHARS = 3000;  // Wikipedia 抜粋の上限（Groq無料枠 TPM=8,000トークン/分 対策）
const DEBUG = false;             // true にすると対象台本などの詳細ログが出る

let LAST_AUTO_FIX_ERROR = "";    // autoFixScript が失敗した理由（Discord に伝える用）

function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

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
    if (payload.build_from_saved) {
      requestBody.inputs.build_from_saved = true;
    }
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

    // ── 内部カスケード呼び出し（チェック/修正の段階実行）──────────
    // Discord からの署名付きリクエストではない。INTERNAL_SECRET で認証する。
    if (request.headers.get('x-vm-internal')) {
      if (env.INTERNAL_SECRET && request.headers.get('x-vm-internal') === env.INTERNAL_SECRET) {
        const state = JSON.parse(await request.text());
        console.log(`[INTERNAL] accepted step=${state.step} round=${state.round}`);
        ctx.waitUntil(runFactStep(new URL(request.url).origin, env, state));
        return new Response('ok');
      }
      // ヘッダーはあるのに一致しない → 環境変数の未設定/不一致（ここを知るのが大事）
      console.error('cascade rejected: INTERNAL_SECRET が未設定または一致しません');
      return new Response('Forbidden', { status: 403 });
    }

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

    // 2. スラッシュコマンド実行 (/fact, /video)
    if (interaction.type === 2) {
      if (interaction.data.name === 'fact') {
        // チェック=ステップ1。不合格なら自分自身へのPOSTで「修正→再チェック」を続ける
        ctx.waitUntil(runFactStep(new URL(request.url).origin, env, {
          step: 'check', round: 1, selector: 'stamped', channelId: channelId
        }));
        return jsonResponse({
          type: 4,
          data: { content: '🛡️ ファクトチェックを開始しました。不合格なら AI の自動修正→再チェックも続けます。経過はスレッドに投稿されます。', flags: 64 }
        });
      }

      // /video: いつでも動画作成ワークフローを起動（ボタンが見つからないときの入口）
      // スレッドに動画クリップ等の添付があればそれを使い、無ければ自動画像生成にフォールバックする
      if (interaction.data.name === 'video') {
        ctx.waitUntil(triggerGitHubWorkflow(env, 'generate_video.yml', { thread_id: channelId, build_from_saved: true }));
        return jsonResponse({
          type: 4,
          data: { content: '🎬 保存済みの音声・字幕＋スレッドに添付された動画/画像で ffmpeg 合成を開始しました！完了したらスレッドに通知します。', flags: 64 }
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
          data: { content: '🚀 素材作成ワークフロー (generate_video.yml / assets_only) を起動しました！完了したらスレッドにお知らせします。', flags: 64 }
        });
      }

      // 再ファクトチェックボタン（スタンプ不問で最新台本を対象に、AI自動修正ループも実行）
      if (customId === 'btn_fact_recheck') {
        ctx.waitUntil(runFactStep(new URL(request.url).origin, env, {
          step: 'check', round: 1, selector: 'latest', channelId: channelId
        }));
        return jsonResponse({
          type: 4,
          data: { content: '🛡️ 最新の台本を再チェック中。経過はスレッドに投稿されます。', flags: 64 }
        });
      }
    }

    console.error(`[INTERACTION] unknown: type=${interaction.type} name=${interaction?.data?.name} (worker.js を最新版に更新してください)`);
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

// 再チェックボタン・修正後チェーン用: スタンプ不問で「最新の台本っぽいコードブロック」を探す
// ※ 台本メッセージは必ずコードブロックで投稿される。❌/⚠️ などのステータス投稿
//   （コードブロック無し）まで台本と誤認する事故を防ぐため、ここではコードブロック必須。
function pickLatestScriptAny(messages) {
  const STATUS_PREFIXES = ["❌", "⚠️", "🔁", "🛡️", "🔍", "🚀"]; // ボットのステータス投稿
  for (const msg of messages) {  // Discord API は新しい順に返す
    const content = msg.content || "";
    if (isMetaMessage(content)) continue;
    if (STATUS_PREFIXES.some(p => content.trimStart().startsWith(p))) continue;
    if (!content.includes("```")) continue; // ステータス投稿などを除外
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
    return extract.length > WIKI_TEXT_MAX_CHARS ? extract.slice(0, WIKI_TEXT_MAX_CHARS) : extract;
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

// ---------------------------------------------------------------- ファクトチェック ステップ実行（カスケード）

// 次ステップを「自分自身へのPOST」で起動する。
// 子呼び出しが新しい30秒枠を持つので、長い待機をしても安全。
//
// ※ 注意: Cloudflare Workers は「自分自身の公開 URL への fetch」を許していない
//   (無限再帰防止のため 404 になる)。次の2つのどちらかが必要:
//   a) Service Binding（Settings → Bindings → Service binding, 名前 SELF, 対象=自分自身）
//   b) 互換フラグ global_fetch_strictly_public（Settings → Compatibility flags で ON）
//   a) があればそちらを優先し、無ければ b) のフラグ下で公開 URL に POST する。
async function cascadeStep(origin, env, state) {
  const headers = {
    'x-vm-internal': env.INTERNAL_SECRET,
    'Content-Type': 'application/json'
  };
  const init = { method: 'POST', headers, body: JSON.stringify(state) };

  try {
    let res;
    if (env.SELF && typeof env.SELF.fetch === 'function') {
      // Service Binding 経由（公開URLを経由しないので最も確実）
      res = await env.SELF.fetch('https://internal.fact-cascade/', init);
      console.log(`[CASCADE] via SERVICE_BINDING step=${state.step} round=${state.round} -> HTTP ${res.status}`);
    } else {
      res = await fetch(origin + '/', init);
      console.log(`[CASCADE] via PUBLIC_URL step=${state.step} round=${state.round} -> HTTP ${res.status}`);
    }
    return res;
  } catch (e) {
    console.error(`[CASCADE] exception: ${e.message}`);
    return new Response('cascade-error', { status: 500 });
  }
}

// ステップ1つをこの呼び出し内で完結させる（必ず30秒内に終わる仕事にする）
async function runFactStep(origin, env, state) {
  try {
    console.log(`[FACT-STEP] start step=${state.step} round=${state.round} selector=${state.selector || "-"}`);
    const messages = await fetchMessages(state.channelId, env);
    if (!messages) {
      console.error(`[FACT-STEP] fetchMessages failed`);
      await postToThread(state.channelId, env, "⚠️ メッセージ一覧の取得に失敗しました。");
      return;
    }
    console.log(`[FACT-STEP] messages fetched: ${messages.length}`);

    // 台本の取り出し: チェックの初回のみ選び方を指定可能、修正後は常に「最新の台本」
    let script = "";
    if (state.step === 'check' && state.round === 1 && state.selector === 'stamped') {
      script = pickScriptByStamp(messages) || pickLatestUserScript(messages);
    } else {
      script = pickLatestScriptAny(messages) || pickScriptByStamp(messages);
    }

    if (!script) {
      console.error(`[FACT-STEP] script not found (step=${state.step} round=${state.round})`);
      await postToThread(state.channelId, env,
        "⚠️ 対象メッセージが見つかりませんでした。チェックしたいメッセージにスタンプを付けてから `/fact` を実行してください。");
      return;
    }

    console.log(`[FACT-STEP] script selected: len=${script.length}`);

    const maxChecks = state.maxRounds || (1 + MAX_AUTO_FIX_ROUNDS);

    if (state.step === 'check') {
      const wikiText = await fetchWikiArticleText(findMetaArticle(messages));
      console.log(`[FACT-STEP] groq check start (wiki=${wikiText.length} chars)`);
      const check = await checkWithGroq(script, env.GROQ_API_KEY, wikiText);
      console.log(`[FACT-STEP] groq check done ok=${check.ok} apiError=${Boolean(check.apiError)}`);

      if (check.ok) {
        const note = state.round > 1 ? `（🛠️ AI自動修正 ${state.round - 1} 回目で通過）` : "";
        await postCheckedMessage(state.channelId, env, script, note);
        return; // チェーン終了
      }

      // API 障害時は台本の問題ではなく再試行を促して終了
      if (check.apiError) {
        await postToThread(state.channelId, env,
          `⚠️ **ファクトチェックの実行に失敗しました**（台本の問題ではありません）\n${check.reason}`);
        return;
      }

      await postToThread(state.channelId, env, `❌ **ファクトチェック不合格** (${state.round}/${maxChecks})\n${check.reason}`);

      // 上限に達していなければ「修正」ステップへカスケード
      if (state.round < maxChecks) {
        const r = await cascadeStep(origin, env, {
          step: 'fix',
          round: state.round,
          channelId: state.channelId,
          reason: check.reason
        });
        if (!r.ok) {
          await postToThread(state.channelId, env,
            "⚠️ AI自動修正ステップの起動に失敗しました（内部認証エラーの可能性）。\nCloudflare の Worker 環境変数 `INTERNAL_SECRET` が設定済み・一致しているか確認してください。");
        }
      } else {
        await postRecheckRequest(state.channelId, env, script);
      }
      return;
    }

    if (state.step === 'fix') {
      const wikiText = await fetchWikiArticleText(findMetaArticle(messages));
      console.log(`[FACT-STEP] groq autofix start (wiki=${wikiText.length} chars)`);
      const revised = await autoFixScript(script, state.reason || "", wikiText, env.GROQ_API_KEY);
      console.log(`[FACT-STEP] groq autofix done revised=${Boolean(revised)}${revised ? ` len=${revised.length}` : ""}`);

      if (!revised) {
        await postToThread(state.channelId, env,
          `⚠️ AI自動修正に失敗しました（${LAST_AUTO_FIX_ERROR || "原因不明"}）。手動で修正し、スタンプを付けて \`/fact\` を実行してください。`);
        await postRecheckRequest(state.channelId, env, script);
        return;
      }

      // 修正版を投稿。prepare_trivia.py は「スレッド内の最新コードブロック」を台本として採用するため、
      // ここで投稿しておけば、この台本がそのまま動画生成に使われる。
      await postToThread(state.channelId, env,
        `🧪 **AI自動修正版 (${state.round}/${MAX_AUTO_FIX_ROUNDS})** 違和感があれば、この台本をコードブロックで編集して返信投稿してください。\n\`\`\`\n${revised}\n\`\`\``);

      // 修正版をチェックステップへ戻す（最新コードブロックが対象になる）
      const r = await cascadeStep(origin, env, {
        step: 'check',
        round: state.round + 1,
        channelId: state.channelId,
        selector: 'latest',
        maxRounds: state.maxRounds
      });
      if (!r.ok) {
        await postToThread(state.channelId, env,
          "⚠️ 再チェックステップの起動に失敗しました（内部認証エラーの可能性）。\nCloudflare の Worker 環境変数 `INTERNAL_SECRET` が設定済み・一致しているか確認してください。");
      }
      return;
    }
  } catch (err) {
    console.error("runFactStep fatal error:", err);
    await postToThread(state.channelId, env,
      `⚠️ ファクトチェック処理中に例外が発生しました:\n\`\`\`${err.message}\`\`\``);
  }
}

// Groq の応答（JSON になっていないことがある）から安全に JSON を取り出す
function extractJsonObject(reply) {
  if (!reply) return null;
  try { return JSON.parse(reply); } catch (e) {}
  const stripped = String(reply)
    .replace(/^```[a-z]*\s*\n?/i, "")
    .replace(/\n?```\s*$/i, "")
    .trim();
  try { return JSON.parse(stripped); } catch (e) {}
  const m = stripped.match(/\{[\s\S]*\}/);
  if (m) {
    try { return JSON.parse(m[0]); } catch (e) {}
  }
  return null;
}

// Groq 呼び出しの共通部分（各ステップ=1呼び出しに分かれているため、単一呼び出し内では待機しない）
async function groqChat(apiKey, systemPrompt, userPrompt, options = {}) {
  const modes = options.forceJson === false ? [false] : [true, false];
  let lastError = "";

  for (const useJsonMode of modes) {
    try {
      const body = {
        model: GROQ_MODEL,
        messages: [
          { role: 'system', content: systemPrompt },
          { role: 'user', content: userPrompt }
        ],
        temperature: options.temperature ?? 0.2,
      };
      if (useJsonMode) body.response_format = { type: "json_object" };

      const res = await fetch('https://api.groq.com/openai/v1/chat/completions', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${apiKey}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(body)
      });

      if (!res.ok) {
        const errText = await res.text();
        lastError = `Groq API エラー (${res.status}): ${errText.slice(0, 200)}`;
        console.error(`groqChat (${useJsonMode ? "json_mode" : "no_format"}):`, lastError);
        continue; // JSON モードなしで再試行
      }

      const data = await res.json();
      if (!data.choices || !data.choices[0] || !data.choices[0].message) {
        lastError = 'Groq API からの応答フォーマットが不正です。';
        console.error('Groq Unexpected Response Structure:', JSON.stringify(data));
        continue;
      }

      return data.choices[0].message.content;
    } catch (e) {
      lastError = `Groq 処理例外: ${e.message}`;
      console.error("groqChat Exception:", e);
    }
  }

  throw new Error(lastError || "不明な Groq エラー");
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
    const reply = await groqChat(apiKey, systemPrompt, userPrompt);
    const parsed = extractJsonObject(reply);
    if (parsed && typeof parsed === "object") {
      return { ok: Boolean(parsed.ok), reason: parsed.reason || "" };
    }
    console.error('checkWithGroq: JSON 抽出失敗:', String(reply).slice(0, 200));
    return { ok: false, apiError: true, reason: "Groq 応答から判定結果を取り出せませんでした。" };
  } catch (e) {
    return {
      ok: false,
      apiError: true, // AI修正を走らせるべきでない「API障害」のマーク
      reason: `${e.message}\n（Groq が一時的に不調の可能性があります。少し待ってから 🔁 再チェックを実行してください）`
    };
  }
}

// 🛠️ AI修正: 指摘を解消する修正版台本を生成する
// 方針: 「安全側に倒して全部ぼかす」ではなく、原文に裏付けのある生きた表現は保持する
const AUTO_FIX_SYSTEM_PROMPT = `あなたはYouTube Shorts向けの歴史雑学台本のファクトチェック指摘を解消する台本作家です。
最重要目標は「ファクトチェックに通ること」と、忘れてはならない「おもしろい物語のままにしておくこと」です。
指摘された部分だけを書き換え、完成版台本を1つだけ出力してください。

【最重要の方針（必ず守る）】
- 元の台本の面白さ・テンポ・問いかけ・感情描写・具体的数字は、指摘と抵触する部分を除き**そのまま保持する**。
- 文面を整えるための全面改稿はしない。「とされています」「言われます」で全体を包む、無個性な文に書き換えることは禁止。
- 【資料】に裏付けがある情報（人数・年号・固有名詞・出来事・場面描写）は、ぼかしを追加せず生のまま使ってよい（むしろ使うべき）。
- 直すのは次の2種類だけ:
  1. 【資料】と矛盾・あるいは【資料】に根拠がないのに断定している部分 → 資料の内容に合わせるか、最小限のぼかしにする
  2. ファクトチェックの指摘が具体的に指している文 → その文だけ直す
- 資料が「（なし）」の場合は、明らかな歴史的誤実のみを最小限修正し、それ以外は元の台本をほぼそのまま出力する。

【形式ルール】
- 「です・ます調」の親しみやすいトーンを維持する。
- 入力の台本が ```json の「scenes」構成（30秒シーン構成）だった場合は、**その JSON 全体の構造を保持したまま** scene_index・duration_sec などのキー名とシーン数を壊さず、narration / screen_text の文言だけを最小限修正して、同じ ```json コードブロック形式で出力する。
- 平文の台本だった場合は、全体を300〜340文字（1文35文字以内・起承転結）で維持する。
- 新しい事実・数字を捏造しない（【資料】内の情報の範囲でのみ修正）。
- 出力は台本（JSON の場合は JSON 全体）のみ。前置き・後書き・引用符は付けない。`;

async function autoFixScript(scriptText, feedback, wikiText, apiKey) {
  LAST_AUTO_FIX_ERROR = "";
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
    let text = await groqChat(apiKey, AUTO_FIX_SYSTEM_PROMPT, userPrompt, { forceJson: false });
    text = text.trim();

    // 念のため前後のコードブロックや前置きを剥ぐ
    text = text.replace(/^```[a-z]*\s*\n?/i, "").replace(/\n?```\s*$/i, "").trim();
    text = text.replace(/^(修正後の台本|台本)[:：]\s*/i, "").trim();

    // 台本として明らかに破綻している出力は取り込まない保険
    if (text.length < MIN_SCRIPT_CHARS || text.length > SCRIPT_MAX_CHARS + 200) {
      LAST_AUTO_FIX_ERROR = `出力の長さが不自然 (${text.length}文字)`;
      console.error(`autoFixScript: ${LAST_AUTO_FIX_ERROR}`);
      return null;
    }
    return text;
  } catch (e) {
    LAST_AUTO_FIX_ERROR = String(e.message).slice(0, 200);
    console.error("autoFixScript exception:", e);
    return null;
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
          label: "📦 素材（音声・台本・プロンプト）を作成する",
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
