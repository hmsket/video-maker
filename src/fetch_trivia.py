import os
import random
import requests
import google.generativeai as genai
import json
import discord

# 環境変数の読み込み
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
FORUM_CHANNEL_ID = int(os.environ.get("FORUM_CHANNEL_ID", "0"))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

def fetch_random_unusual_article():
    """Wikipedia APIから珍項目を取得"""
    url = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "format": "json",
        "prop": "extracts",
        "generator": "categorymembers",
        "gcmpageid": 4833299,
        "gcmlimit": 50,
        "exintro": True,
        "explaintext": True,
    }
    headers = {'User-Agent': 'VideoMakerBot/1.0'}
    response = requests.get(url, params=params, headers=headers)
    pages = response.json().get("query", {}).get("pages", {})
    if not pages:
        return None
    page = random.choice(list(pages.values()))
    return {
        "title": page.get("title"),
        "extract": page.get("extract", "")[:1000]
    }

def generate_script_and_prompts(article_data):
    """Gemini APIで台本とプロンプトを自動作成"""
    prompt = f"""
以下の歴史事件の記事を読み、YouTube Shorts用の台本とプロンプトを作成してください。

タイトル: {article_data['title']}
概要: {article_data['extract']}

必ず以下のJSON形式のみで出力してください（Markdown記法は除外すること）:
{{
  "title": "日本語タイトル（20文字以内）",
  "summary": "概要（100文字程度）",
  "script": "ナレーション原稿（130〜150文字程度）",
  "image_prompts": [
    "英語プロンプト1",
    "英語プロンプト2"
  ],
  "video_prompts": [
    "動画プロンプト1",
    "動画プロンプト2"
  ]
}}
"""
    model = genai.GenerativeModel('gemini-1.5-flash')
    response = model.generate_content(prompt)
    text = response.text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.endswith("```"):
        text = text[:-3]
    return json.loads(text.strip())

# Discord Bot経由でフォーラムにスレッド作成
class DiscordClient(discord.Client):
    def __init__(self, trivia_data):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.trivia_data = trivia_data

    async def on_ready(self):
        print(f'Logged in as {self.user}')
        channel = self.get_channel(FORUM_CHANNEL_ID)
        
        if channel and isinstance(channel, discord.ForumChannel):
            content = f"**【概要】**\n{self.trivia_data['summary']}\n\n" \
                      f"**【台本（VOICEVOX）】**\n{self.trivia_data['script']}\n\n" \
                      f"**【画像プロンプト】**\n1. `{self.trivia_data['image_prompts'][0]}`\n2. `{self.trivia_data['image_prompts'][1]}`\n\n" \
                      f"**【動画プロンプト】**\n1. `{self.trivia_data['video_prompts'][0]}`\n2. `{self.trivia_data['video_prompts'][1]}`"
            
            # フォーラム内に新しいスレッドを立てる
            thread = await channel.create_thread(
                name=f"💡 {self.trivia_data['title']}",
                content=content
            )
            print(f"✅ スレッド作成完了: {thread.thread.name}")
        else:
            print("❌ フォーラムチャンネルが見つからないか、種別が異なります。")
            
        await self.close()

if __name__ == "__main__":
    print("🔍 ネタを取得中...")
    article = fetch_random_unusual_article()
    if article:
        print(f"📌 {article['title']} から台本を作成中...")
        data = generate_script_and_prompts(article)
        
        print("🚀 Discordのフォーラムに投稿中...")
        client = DiscordClient(data)
        client.run(DISCORD_TOKEN)
