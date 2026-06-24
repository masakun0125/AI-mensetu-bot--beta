import os
import discord
from discord import app_commands
from discord.ext import commands
from groq import Groq
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import json
from datetime import datetime
from enum import Enum

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CATEGORY_ID = int(os.getenv("INTERVIEW_CATEGORY_ID", "0"))

client = Groq(api_key=GROQ_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 面接セッション管理
interview_sessions = {}

class InterviewPhase(Enum):
    AVAILABILITY = "availability"  # 業務可能な時間帯
    MOTIVATION = "motivation"  # 志望動機
    SELF_PR = "self_pr"  # 自己PR
    TROLL_RESPONSE = "troll_response"  # 荒らし対応
    COMPLETED = "completed"  # 完了

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
    def __init__(self, user_id, user_name):
        self.user_id = user_id
        self.user_name = user_name
        self.start_time = datetime.now()
        
        # 各フェーズのデータ
        self.phase_data = {
            InterviewPhase.AVAILABILITY: {"answer": "", "follow_up_count": 0},
            InterviewPhase.MOTIVATION: {"answer": "", "follow_up_count": 0},
            InterviewPhase.SELF_PR: {"answer": "", "follow_up_count": 0},
            InterviewPhase.TROLL_RESPONSE: {"answer": "", "follow_up_count": 0},
        }
        
        # 会話履歴
        self.conversation_history = []
        
        # 現在のフェーズ
        self.current_phase = InterviewPhase.AVAILABILITY

    def add_message(self, user_msg, ai_response):
        """会話を記録"""
        self.conversation_history.append({
            "user": user_msg,
            "ai": ai_response,
            "timestamp": datetime.now().isoformat(),
            "phase": self.current_phase.value
        })

    def record_phase_answer(self, answer):
        """現在のフェーズの回答を記録"""
        self.phase_data[self.current_phase]["answer"] = answer

    def increment_follow_up(self):
        """フォローアップ質問数を増加"""
        self.phase_data[self.current_phase]["follow_up_count"] += 1

    def next_phase(self):
        """次のフェーズに移動"""
        phases = list(InterviewPhase)[:-1]  # COMPLETED以外
        current_idx = phases.index(self.current_phase)
        if current_idx < len(phases) - 1:
            self.current_phase = phases[current_idx + 1]
            return True
        else:
            self.current_phase = InterviewPhase.COMPLETED
            return False

class InterviewForm(discord.ui.Modal, title="面接 申込フォーム"):
    pass

class StartButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="面接を申し込む", style=discord.ButtonStyle.green, custom_id="start_interview")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await start_interview_process(interaction)

async def start_interview_process(interaction: discord.Interaction):
    """面接開始プロセス"""
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

    session = InterviewSession(interaction.user.id, interaction.user.name)
    interview_sessions[interview_channel.id] = session

    embed = discord.Embed(title="モデレーター募集面接へようこそ", color=discord.Color.blue())
    embed.add_field(
        name="面接内容",
        value="以下の4つの質問にお答えいただきます：\n1. 業務可能な時間帯\n2. 志望動機\n3. 自己PR\n4. 荒らし対応方法\n※何問か追加の質問をされる場合があります。\n※面接は必ずご本人が回答してください。/n※AIなどの補助ツールを使用した場合、今後の応募をお断りする場合があります。",
        inline=False
    )
    await interview_channel.send(embed=embed)

    initial_question = "まずは、**あなたが業務可能な時間帯**を教えていただけますか？\n例）平日は夜間のみ、土日は終日可能、など"
    await interview_channel.send(initial_question)

    await interaction.followup.send(
        f"面接チャンネルを作成しました！ {interview_channel.mention} へ移動してください。",
        ephemeral=True
    )

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
        title="<<=面接申込窓口=>>",
        description="下のボタンを押して、モデレーター募集面接に申し込んでください。この面接はBotが対応します。",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, view=StartButton())

def get_follow_up_prompt(phase: InterviewPhase, user_answer: str) -> str:
    """フェーズに応じたフォローアップ質問を生成"""
    if phase == InterviewPhase.AVAILABILITY:
        return f"""ユーザーの回答: "{user_answer}"

ユーザーの業務可能な時間帯について、以下のいずれかのフォローアップ質問を1つだけ選んで、簡潔に質問してください：
- 曜日や時間帯がより具体的でしたら、詳しく教えていただけますか?
- 期間はどのくらい継続できる予定ですか?
- または、回答が十分に詳しい場合は、その旨を伝えて次に進む準備ができたと述べてください。

簡潔に、1-2文で返してください。"""
    
    elif phase == InterviewPhase.MOTIVATION:
        return f"""ユーザーの回答: "{user_answer}"

ユーザーの志望動機について、以下のいずれかのフォローアップ質問を1つだけ選んで、簡潔に質問してください：
- なぜモデレーターという役割に興味を持ったのですか?
- 過去に何か類似した経験やコミュニティ活動はありますか?
- または、回答が十分に詳しい場合は、その旨を伝えて次に進む準備ができたと述べてください。

簡潔に、1-2文で返してください。"""
    
    elif phase == InterviewPhase.SELF_PR:
        return f"""ユーザーの回答: "{user_answer}"

ユーザーの自己PRについて、具体的な例を引き出すフォローアップ質問を1つだけ選んで、簡潔に質問してください：
- その強みは、具体的にどのような場面で活かせますか?（例：スポーツ、学業、アルバイト等で何を頑張ったのか）
- その経験から得た学びは何ですか?
- または、回答が十分に詳しい場合は、その旨を伝えて次に進む準備ができたと述べてください。

簡潔に、1-2文で返してください。"""
    
    elif phase == InterviewPhase.TROLL_RESPONSE:
        return f"""ユーザーの回答: "{user_answer}"

ユーザーの荒らし対応方法について、以下のいずれかのフォローアップ質問を1つだけ選んで、簡潔に質問してください：
- その対応方法を取る理由は何ですか?
- 実際にそのような場面に遭遇したことはありますか?
- または、回答が十分に詳しい場合は、その旨を伝えて次に進む準備ができたと述べてください。

簡潔に、1-2文で返してください。"""
    
    return ""

def get_phase_next_question(phase: InterviewPhase) -> str:
    """次のフェーズの質問を取得"""
    if phase == InterviewPhase.MOTIVATION:
        return "ありがとうございます。次に、**モデレーターへの志望動機**を教えていただけますか？\nなぜこの職に応募しようと思ったのか、ご説明ください。"
    elif phase == InterviewPhase.SELF_PR:
        return "ありがとうございます。次に、**あなたの自己PR**をお願いします。\nあなたの強み、得意なこと、経験などをお聞かせください。"
    elif phase == InterviewPhase.TROLL_RESPONSE:
        return "ありがとうございます。最後に、**コミュニティで荒らしやルール違反を見かけた時の対応方法**を教えていただけますか？\nどのように対応すべきだと考えますか？"
    elif phase == InterviewPhase.COMPLETED:
        return None
    return ""

async def generate_ai_response(session: InterviewSession, user_message: str) -> str:
    """Groq APIを使ってAI応答を生成"""
    phase = session.current_phase
    phase_info = session.phase_data[phase]
    
    session.record_phase_answer(user_message)
    
    if phase_info["follow_up_count"] < 1:
        prompt = get_follow_up_prompt(phase, user_message)
        phase_info["follow_up_count"] += 1
        
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.7
        )
        
        ai_response = response.choices[0].message.content
        
        if any(kw in ai_response for kw in ["次に進む", "次へ", "ありがとうございました", "了承しました"]):
            session.next_phase()
            if session.current_phase != InterviewPhase.COMPLETED:
                next_q = get_phase_next_question(session.current_phase)
                return f"{ai_response}\n\n{next_q}"
        
        return ai_response
    else:
        session.next_phase()
        if session.current_phase != InterviewPhase.COMPLETED:
            next_q = get_phase_next_question(session.current_phase)
            return f"ありがとうございました。\n\n{next_q}"
        else:
            return "すべてのご質問にお答えいただき、ありがとうございました。\n結果はこのbotよりDMにてお知らせします。しばらくお待ちください。また、面接の合否についてのお問い合わせは一切承っておりません。"

async def generate_evaluation(session: InterviewSession) -> dict:
    """面接全体を評価（厳格化プロンプト）"""
    conversation_text = "\n".join([
        f"【{msg['phase']}】\nユーザー: {msg['user']}"
        for msg in session.conversation_history
    ])
    
    evaluation_prompt = f"""
【厳格な採用面接官による総合評価】

あなたは非常に厳しい採用基準を持つ面接官です。
モデレーターは重要な役職であり、曖昧・短すぎる回答や熱意が感じられない応募者は不合格にしてください。
恐らく合計で応募してくる人数は3人から5人を予定しているので、厳格すぎて一人も合格者がでないという状況は避けたいです。そのため、明らかに熱意がない人は不合格、熱意はあるが微妙な人は要検討などにするのが良いと思います。

【採点基準】
- 回答が1〜2文程度の短いもの → 各項目 3点以下
- 具体性・根拠がない回答 → 各項目 4点以下
- 熱意・コミュニティへの貢献意欲が感じられない → motivation 3点以下
- 荒らし対応が「優しく対応」など曖昧なもの → judgment 3点以下
- 合計24点未満 → 不合格
- 合計25〜31点 → 要検討
- 合計32点以上 → 合格

【面接者】
{session.user_name}

【面接の回答】
{conversation_text}

【評価（JSON形式のみで返してください）】
{{
  "availability": {{"score": 0-10, "comment": "コメント"}},
  "motivation": {{"score": 0-10, "comment": "コメント"}},
  "personality": {{"score": 0-10, "comment": "コメント"}},
  "judgment": {{"score": 0-10, "comment": "コメント"}},
  "overall_score": 0-40,
  "recommendation": "合格 / 要検討 / 不合格",
  "summary": "総合コメント（150字以内）"
}}
"""
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": evaluation_prompt}],
            max_tokens=500,
            temperature=0.5
        )
        
        response_text = response.choices[0].message.content
        
        if "```json" in response_text:
            json_str = response_text.split("```json")[1].split("```")[0]
        elif "{" in response_text:
            start = response_text.find("{")
            end = response_text.rfind("}") + 1
            json_str = response_text[start:end]
        else:
            json_str = response_text
        
        return json.loads(json_str)
    except Exception as e:
        print(f"Evaluation error: {e}")
        return {
            "overall_score": 0,
            "recommendation": "要検討",
            "summary": "評価生成エラー",
            "raw_response": str(e)
        }

# 合否通知ボタンビュー
class ResultView(discord.ui.View):
    def __init__(self, session: InterviewSession, guild: discord.Guild):
        super().__init__(timeout=None)
        self.session = session
        self.guild = guild

    # 修正箇所: send_dmメソッドの刷新
    async def send_dm(self, interaction: discord.Interaction, result: str, color: discord.Color, message: str):
        try:
            user = await interaction.guild.fetch_member(self.session.user_id)  # ← get_member → fetch_member に変更
        except discord.NotFound:
            await interaction.response.send_message("⚠️ ユーザーがサーバーから退出しているため通知できませんでした。", ephemeral=True)
            return
        
        try:
            embed = discord.Embed(
                title="**<<面接結果のお知らせ>>**",
                description=message,
                color=color
            )
            embed.set_footer(text=f"{self.guild.name} モデレーター採用面接")
            await user.send(embed=embed)
            await interaction.response.send_message(f"✅ {user.name} にDMで{result}を通知しました。", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("⚠️ ユーザーのDMが無効のため通知できませんでした。", ephemeral=True)
        
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="✅ 合格", style=discord.ButtonStyle.green)
    async def pass_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.send_dm(
            interaction, "合格",
            discord.Color.green(),
            "**モデレーター採用面接の結果をお知らせします。**\n\nこの度は面接にご参加いただきありがとうございました。\n審査の結果、__**合格**__となりました。おめでとうございます！\n近日中に担当者よりご連絡いたします。"
        )

    @discord.ui.button(label="❌ 不合格", style=discord.ButtonStyle.red)
    async def fail_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.send_dm(
            interaction, "不合格",
            discord.Color.red(),
            "**モデレーター採用面接の結果をお知らせします。**\n\nこの度は面接にご参加いただきありがとうございました。\n慎重に審査した結果、今回は__**不採用**__とさせていただきました。\nまたの機会にぜひご応募ください。"
        )

    @discord.ui.button(label="🔄 保留", style=discord.ButtonStyle.grey)
    async def pending_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.send_dm(
            interaction, "保留",
            discord.Color.yellow(),
            "**モデレーター採用面接の結果をお知らせします。**\n\nこの度は面接にご参加いただきありがとうございました。\n現在審査中のため、結果は__**保留**__となっております。\n追って担当者よりご連絡いたします。"
        )

# /end_interview コマンド（管理者限定化 ＆ 権限剥奪 ＆ ボタン付き送信）
@bot.tree.command(name="end_interview", description="面接を終了し、評価結果を表示します（管理者専用）")
@app_commands.checks.has_permissions(administrator=True)
async def end_interview(interaction: discord.Interaction):
    channel_id = interaction.channel_id

    if channel_id not in interview_sessions:
        await interaction.response.send_message("このチャンネルは面接チャンネルではありません。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    session = interview_sessions[channel_id]

    if len(session.conversation_history) == 0:
        await interaction.followup.send("まだ回答がありません。", ephemeral=True)
        return

    # ユーザーの閲覧権限を剥奪
    member = interaction.guild.get_member(session.user_id)
    if member:
        await interaction.channel.set_permissions(member, read_messages=False, send_messages=False)

    thinking_msg = await interaction.followup.send("評価を生成中です...", ephemeral=True)

    try:
        evaluation = await generate_evaluation(session)

        recommendation_color = {
            "合格": discord.Color.green(),
            "要検討": discord.Color.yellow(),
            "不合格": discord.Color.red()
        }.get(evaluation.get("recommendation", "要検討"), discord.Color.blue())

        embed = discord.Embed(
            title="面接評価結果",
            description=f"**AI推奨: {evaluation.get('recommendation', '要検討')}**",
            color=recommendation_color
        )
        embed.add_field(name="総合スコア", value=f"{evaluation.get('overall_score', 0)}/40", inline=False)

        for key, label in [("availability", "業務対応可能性"), ("motivation", "志望度"), ("personality", "人格・適性"), ("judgment", "判断力・対応力")]:
            if key in evaluation:
                embed.add_field(
                    name=label,
                    value=f"**{evaluation[key].get('score', 0)}/10** - {evaluation[key].get('comment', '')}",
                    inline=False
                )

        embed.add_field(name="総評", value=evaluation.get("summary", ""), inline=False)
        embed.set_footer(text=f"面接者: {session.user_name}")

        # エフェメラルな考え中メッセージを削除し、チャンネルに結果とボタンを送信
        await thinking_msg.delete()
        await interaction.channel.send(embed=embed, view=ResultView(session, interaction.guild))

    except Exception as e:
        await interaction.followup.send(f"⚠️Error: {str(e)}", ephemeral=True)
        print(e)

# エラーハンドリング
@end_interview.error
async def end_interview_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("このコマンドは管理者のみ使用できます。", ephemeral=True)

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.channel.name.startswith("面接-"):
        async with message.channel.typing():
            try:
                if message.channel.id not in interview_sessions:
                    return
                
                session = interview_sessions[message.channel.id]
                
                if session.current_phase == InterviewPhase.COMPLETED:
                    await message.channel.send("面接は既に完了しています。管理者が確認するまでお待ちください。")
                    return
                
                response_text = await generate_ai_response(session, message.content)
                session.add_message(message.content, response_text)
                await message.channel.send(response_text)
            
            except Exception as e:
                await message.channel.send(f"⚠️Error\n内容: `{str(e)}`")
                print(e)

    await bot.process_commands(message)

threading.Thread(target=run_web_server, daemon=True).start()
bot.run(TOKEN)
