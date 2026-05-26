import os
import discord
from discord import app_commands
from discord.ext import commands
from google import genai  # 最新のライブラリ
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# --- 環境変数の読み込み ---
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CATEGORY_ID = int(os.getenv("INTERVIEW_CATEGORY_ID", "0"))  # 面接チャンネルを作るカテゴリのID

# APIバージョンを「v1」に完全固定して初期化
ai = genai.Client(api_key=GEMINI_API_KEY, http_options={'api_version': 'v1'})

# Botの初期化
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Render用のWebサーバー設定（スリープ・エラー防止用） ---
class WebServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

def run_web_server():
    server = HTTPServer(("0.0.0.0", 8080), WebServer)
    server.serve_forever()

# --- 入力フォーム（モーダル）の定義 ---
class InterviewForm(discord.ui.Modal, title="面接 申込フォーム"):
    time_slot = discord.ui.TextInput(label="オンラインになれる時間帯", placeholder="例：平日夜、土日など", max_length=100)
    rule_reply = discord.ui.TextInput(label="ルール違反を見かけた際の対応", style=discord.TextStyle.paragraph, placeholder="どのように声をかけるか記述してください", max_length=300)
    reason = discord.ui.TextInput(label="志望動機", style=discord.TextStyle.paragraph, placeholder="なぜ応募したか", max_length=500)
    pr = discord.ui.TextInput(label="自己PR", style=discord.TextStyle.paragraph, placeholder="あなたの強みなど", max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        category = guild.get_channel(CATEGORY_ID) if CATEGORY_ID else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        channel_name = f"面接-{interaction.user.name}"
        interview_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites
        )

        embed = discord.Embed(title="📝 面接申込内容", color=discord.Color.blue())
        embed.add_field(name="申請者", value=interaction.user.mention, inline=False)
        embed.add_field(name="時間帯", value=self.time_slot.value, inline=False)
        embed.add_field(name="ルール違反への対応", value=self.rule_reply.value, inline=False)
        embed.add_field(name="志望動機", value=self.reason.value, inline=False)
        embed.add_field(name="自己PR", value=self.pr.value, inline=False)
        
        await interview_channel.send(embed=embed)
        
        welcome_msg = (
            f"それでは{interaction.user.mention}さん、面接を開始します。\n"
            "提出いただいた内容を確認しました。まずは、今回の志望動機について詳しくお伺いできますか？"
        )
        await interview_channel.send(welcome_msg)
        await interaction.followup.send(f"面接チャンネルを作成しました！ {interview_channel.mention} へ移動してください。", ephemeral=True)

# --- 「申し込む」ボタンの定義 ---
class StartButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="面接を申し込む", style=discord.ButtonStyle.green, custom_id="start_interview")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(InterviewForm())

# --- Botのイベント処理 ---
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    bot.add_view(StartButton())
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)

# 面接用パネルを設置するコマンド
@bot.tree.command(name="setup_panel", description="面接申し込み用パネルを設置します")
async def setup_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤝 面接申込窓口",
        description="下のボタンを押して、必要事項を入力すると専用の面接チャンネルが作成されます。",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, view=StartButton())

# 面接チャンネル内でのAIとの会話処理
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.name.startswith("面接-"):
        async with message.channel.typing():
            try:
                # ✨ プロンプトの中に直接役割を埋め込む（エラーを100%回避するスタイル）
                full_prompt = (
                    "【あなたは厳格かつ丁寧な採用面接官です。以下の指示に絶対に従って会話してください】\n"
                    "1. ユーザーの回答に対して深掘りする質問を1問ずつ投げかけてください。\n"
                    "2. 一度にたくさん質問せず、対話を意識してください。\n"
                    "3. 最終的な合否は出さず、面接の対話を続けてください。\n\n"
                    f"ユーザーからの回答: {message.content}"
                )

                # configを使わずシンプルなテキストだけで送信
                response = ai.models.generate_content(
                    model='gemini-1.5-flash',
                    contents=full_prompt
                )
                
                await message.channel.send(response.text)
            except Exception as e:
                await message.channel.send(f"⚠️ AIの応答中にエラーが発生しました。\nエラー内容: `{str(e)}`")
                print(e)

    await bot.process_commands(message)

# サーバー起動とBot起動
threading.Thread(target=run_web_server, daemon=True).start()
bot.run(TOKEN)
