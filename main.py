import os
import discord
from discord import app_commands
from discord.ext import commands
from google import genai
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import json
from datetime import datetime

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CATEGORY_ID = int(os.getenv("INTERVIEW_CATEGORY_ID", "0"))

ai = genai.Client(api_key=GEMINI_API_KEY, http_options={'api_version': 'v1beta'})

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 面接セッション管理
interview_sessions = {}

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

class InterviewSession:
    """面接セッション情報を管理"""
    def __init__(self, user_id, user_name, form_data):
        self.user_id = user_id
        self.user_name = user_name
        self.form_data = form_data
        self.conversation = []
        self.question_count = 0
        self.start_time = datetime.now()
    
    def add_exchange(self, user_msg, ai_response):
        """会話を記録"""
        self.conversation.append({
            "user": user_msg,
            "ai": ai_response,
            "timestamp": datetime.now().isoformat()
        })
        self.question_count += 1

class InterviewForm(discord.ui.Modal, title="面接 申込フォーム"):
    time_slot = discord.ui.TextInput(label="オンラインになれる時間帯", placeholder="例：平日夜、土日など", max_length=100)
    rule_reply = discord.ui.TextInput(label="ルール違反を見かけた際の対応", style=discord.TextStyle.paragraph, placeholder="どのように声をかけるか記述してください", max_length=500)
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

        # セッション作成
        form_data = {
            "time_slot": self.time_slot.value,
            "rule_reply": self.rule_reply.value,
            "reason": self.reason.value,
            "pr": self.pr.value
        }
        interview_sessions[interview_channel.id] = InterviewSession(
            interaction.user.id,
            interaction.user.name,
            form_data
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
        await interview_channel.send("（面接終了後、`/end_interview` コマンドで評価結果を取得できます）")
        await interaction.followup.send(f"面接チャンネルを作成しました！ {interview_channel.mention} へ移動してください。", ephemeral=True)

class StartButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="面接を申し込む", style=discord.ButtonStyle.green, custom_id="start_interview")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(InterviewForm())

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    bot.add_view(StartButton())
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)

@bot.tree.command(name="setup_panel", description="面接申し込み用パネルを設置します")
async def setup_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤝 面接申込窓口",
        description="下のボタンを押して、必要事項を入力すると専用の面接チャンネルが作成されます。",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, view=StartButton())

async def generate_interview_question(user_response, context):
    """AIが面接の質問を生成"""
    full_prompt = (
        "【あなたは厳格かつ丁寧な採用面接官です。以下の指示に絶対に従って会話してください】\n"
        "1. ユーザーの回答に対して深掘りする質問を1問ずつ投げかけてください。\n"
        "2. 一度にたくさん質問せず、対話を意識してください。\n"
        "3. 最終的な合否は出さず、面接の対話を続けてください。\n"
        f"ユーザーからの回答: {user_response}"
    )
    
    response = ai.models.generate_content(
        model='gemini-1.5-flash',
        contents=full_prompt
    )
    
    return response.text

async def generate_evaluation(session):
    """面接全体を評価して合否を判定"""
    conversation_text = "\n".join([
        f"面接官: {ex['ai']}\nユーザー: {ex['user']}"
        for ex in session.conversation
    ])
    
    evaluation_prompt = f"""
【採用面接官の総合評価】

以下の面接記録をもとに、モデレーター職の候補者を評価してください。

【申込情報】
- 時間帯: {session.form_data['time_slot']}
- ルール違反対応: {session.form_data['rule_reply']}
- 志望動機: {session.form_data['reason']}
- 自己PR: {session.form_data['pr']}

【面接の会話】
{conversation_text}

【評価項目（JSON形式で返してください）】
{{
  "technical_skills": {{"score": 0-10, "comment": "技術的スキルの評価"}},
  "communication": {{"score": 0-10, "comment": "コミュニケーション能力"}},
  "motivation": {{"score": 0-10, "comment": "志望度・モチベーション"}},
  "rule_awareness": {{"score": 0-10, "comment": "ルール遵守・規律意識"}},
  "problem_solving": {{"score": 0-10, "comment": "問題解決能力"}},
  "overall_score": 0-50,
  "recommendation": "合格 / 要検討 / 不合格",
  "summary": "総合コメント（100字程度）"
}}

必ずJSON形式で返してください。"""
    
    response = ai.models.generate_content(
        model='gemini-1.5-flash',
        contents=evaluation_prompt
    )
    
    try:
        # JSON部分を抽出
        json_str = response.text
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0]
        elif "{" in json_str:
            start = json_str.find("{")
            end = json_str.rfind("}") + 1
            json_str = json_str[start:end]
        
        evaluation = json.loads(json_str)
    except (json.JSONDecodeError, IndexError) as e:
        print(f"Evaluation JSON parse error: {e}")
        evaluation = {
            "overall_score": 0,
            "recommendation": "要検討",
            "summary": "評価生成エラー",
            "raw_response": response.text
        }
    
    return evaluation

@bot.tree.command(name="end_interview", description="面接を終了し、評価結果を表示します")
async def end_interview(interaction: discord.Interaction):
    """面接終了コマンド"""
    channel_id = interaction.channel_id
    
    if channel_id not in interview_sessions:
        await interaction.response.send_message("このチャンネルは面接チャンネルではありません。", ephemeral=True)
        return
    
    await interaction.response.defer()
    
    session = interview_sessions[channel_id]
    
    if len(session.conversation) == 0:
        await interaction.followup.send("まだ会話がありません。面接をお続けください。")
        return
    
    # 評価生成中...
    thinking_msg = await interaction.followup.send("⏳ 評価を生成中です...（30秒程度かかります）")
    
    try:
        evaluation = await generate_evaluation(session)
        
        # 評価結果の表示
        if "raw_response" in evaluation:
            # エラー時
            embed = discord.Embed(
                title="⚠️ 評価生成エラー",
                description=evaluation["summary"],
                color=discord.Color.red()
            )
            embed.add_field(name="詳細", value=evaluation.get("raw_response", "不明なエラー")[:1024], inline=False)
        else:
            # 成功時
            recommendation_color = {
                "合格": discord.Color.green(),
                "要検討": discord.Color.yellow(),
                "不合格": discord.Color.red()
            }.get(evaluation.get("recommendation", "要検討"), discord.Color.gray())
            
            embed = discord.Embed(
                title="📊 面接評価結果",
                description=f"**推奨: {evaluation.get('recommendation', '要検討')}**",
                color=recommendation_color
            )
            
            embed.add_field(name="総合スコア", value=f"{evaluation.get('overall_score', 0)}/50", inline=False)
            
            # 各項目の詳細スコア
            if "technical_skills" in evaluation:
                embed.add_field(
                    name="技術スキル",
                    value=f"**{evaluation['technical_skills'].get('score', 0)}/10** - {evaluation['technical_skills'].get('comment', '')}",
                    inline=False
                )
            if "communication" in evaluation:
                embed.add_field(
                    name="コミュニケーション能力",
                    value=f"**{evaluation['communication'].get('score', 0)}/10** - {evaluation['communication'].get('comment', '')}",
                    inline=False
                )
            if "motivation" in evaluation:
                embed.add_field(
                    name="志望度・モチベーション",
                    value=f"**{evaluation['motivation'].get('score', 0)}/10** - {evaluation['motivation'].get('comment', '')}",
                    inline=False
                )
            if "rule_awareness" in evaluation:
                embed.add_field(
                    name="ルール遵守意識",
                    value=f"**{evaluation['rule_awareness'].get('score', 0)}/10** - {evaluation['rule_awareness'].get('comment', '')}",
                    inline=False
                )
            if "problem_solving" in evaluation:
                embed.add_field(
                    name="問題解決能力",
                    value=f"**{evaluation['problem_solving'].get('score', 0)}/10** - {evaluation['problem_solving'].get('comment', '')}",
                    inline=False
                )
            
            embed.add_field(name="総評", value=evaluation.get("summary", ""), inline=False)
            
            # メタ情報
            embed.set_footer(text=f"質問数: {session.question_count} | 面接者: {session.user_name}")
        
        await thinking_msg.delete()
        await interaction.followup.send(embed=embed)
        
    except Exception as e:
        await thinking_msg.delete()
        await interaction.followup.send(f"⚠️ 評価生成中にエラーが発生しました: {str(e)}")
        print(e)

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.name.startswith("面接-"):
        async with message.channel.typing():
            try:
                # セッション確認
                if message.channel.id not in interview_sessions:
                    session = InterviewSession(message.author.id, message.author.name, {})
                    interview_sessions[message.channel.id] = session
                
                session = interview_sessions[message.channel.id]
                
                # 質問を生成
                response_text = await generate_interview_question(message.content, session)
                
                # 会話を記録
                session.add_exchange(message.content, response_text)
                
                # 回答を送信
                await message.channel.send(response_text)
            except Exception as e:
                await message.channel.send(f"⚠️ AIの応答中にエラーが発生しました。\nエラー内容: `{str(e)}`")
                print(e)

    await bot.process_commands(message)

threading.Thread(target=run_web_server, daemon=True).start()
bot.run(TOKEN)
