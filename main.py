import os
import io
import discord
from discord.ext import commands
from flask import Flask
from threading import Thread
from googleapiclient.discovery import build
from google.oauth2 import service_account
from zipfile import ZipFile
from datetime import datetime

# ====== KEEP ALIVE SERVER ======
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# ====== DISCORD BOT ======
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))
FOLDER_ID = os.getenv("FOLDER_ID")

if not DISCORD_TOKEN or not DISCORD_CHANNEL_ID:
    raise ValueError("❌ Token/Channel ID belum diset")

# ====== GOOGLE DRIVE SERVICE ACCOUNT ======
def get_drive_service():
    creds_json = os.getenv("GDRIVE_CREDENTIALS")
    if not creds_json:
        raise ValueError("❌ GDRIVE_CREDENTIALS tidak ditemukan")

    import json
    creds_dict = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

# ====== CEK FILE ZIP ======
last_seen = {}

async def check_drive():
    global last_seen
    drive_service = get_drive_service()

    results = drive_service.files().list(
        q=f"'{FOLDER_ID}' in parents and mimeType='application/zip'",
        fields="files(id, name, modifiedTime, createdTime, webViewLink)"
    ).execute()

    files = results.get("files", [])
    channel = bot.get_channel(DISCORD_CHANNEL_ID)

    for f in files:
        file_id = f["id"]
        file_name = f["name"]
        modified = f["modifiedTime"]
        created = f["createdTime"]

        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        modified_dt = datetime.fromisoformat(modified.replace("Z", "+00:00"))

        if file_id not in last_seen:
            last_seen[file_id] = modified
            embed = discord.Embed(
                title=f"📥 {file_name} Added",
                description=f"Upload date: {created_dt.strftime('%Y-%m-%d %H:%M')}",
                color=discord.Color.green()
            )
            embed.add_field(name="Google Drive", value=f"[Open File]({f['webViewLink']})", inline=False)
            await channel.send(embed=embed)

        elif last_seen[file_id] != modified:
            last_seen[file_id] = modified
            embed = discord.Embed(
                title=f"♻️ {file_name} Updated",
                description=f"Updated at: {modified_dt.strftime('%Y-%m-%d %H:%M')}",
                color=discord.Color.orange()
            )
            embed.add_field(name="Google Drive", value=f"[Open File]({f['webViewLink']})", inline=False)
            await channel.send(embed=embed)

# ====== EVENTS ======
@bot.event
async def on_ready():
    print(f"✅ Bot {bot.user} sudah online!")
    await check_drive()  # cek langsung saat bot nyala

@bot.command()
async def gen(ctx):
    await ctx.send("🔍 Mengecek Google Drive...")
    await check_drive()

# ====== START ======
keep_alive()
bot.run(DISCORD_TOKEN)
