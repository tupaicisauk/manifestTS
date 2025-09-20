import os
import json
import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask
from threading import Thread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import datetime

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
bot = commands.Bot(command_prefix="!", intents=intents)

# === Google Drive Setup ===
def get_drive_service():
    creds_json = os.getenv("GDRIVE_CREDENTIALS")
    if not creds_json:
        raise ValueError("❌ GDRIVE_CREDENTIALS tidak ditemukan di environment variables")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds)

FOLDER_ID = os.getenv("FOLDER_ID")

# ====== BOT EVENTS ======
@bot.event
async def on_ready():
    print(f"✅ {bot.user} sudah online")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"❌ Error sync commands: {e}")

# ====== COMMAND: /gen [appid] ======
@bot.tree.command(name="gen", description="Ambil manifest tertentu dari Google Drive (contoh: /gen 10)")
@app_commands.describe(appid="Nomor AppID (misal: 10)")
async def gen(interaction: discord.Interaction, appid: str):
    await interaction.response.defer(thinking=True)

    drive_service = get_drive_service()
    file_name = f"{appid}.zip"

    results = drive_service.files().list(
        q=f"'{FOLDER_ID}' in parents and name='{file_name}'",
        fields="files(id, name, createdTime, modifiedTime, webViewLink)"
    ).execute()
    files = results.get("files", [])

    if not files:
        await interaction.followup.send(f"⚠️ File `{file_name}` tidak ditemukan di Google Drive.")
        return

    file = files[0]
    created = datetime.datetime.fromisoformat(file["createdTime"][:-1])
    modified = datetime.datetime.fromisoformat(file["modifiedTime"][:-1])

    embed = discord.Embed(
        title=f"📦 Manifest Found: {file['name']}",
        description=f"[🔗 Open File]({file['webViewLink']})",
        color=discord.Color.blue()
    )
    embed.add_field(name="Upload date", value=created.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(name="Updated date", value=modified.strftime("%Y-%m-%d"), inline=True)
    embed.set_footer(text="Generated via Google Drive API")

    await interaction.followup.send(embed=embed)

# ====== START ======
keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))
