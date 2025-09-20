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
known_files = {}

# ====== BOT EVENTS ======
@bot.event
async def on_ready():
    print(f"✅ {bot.user} sudah online")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"❌ Error sync commands: {e}")

# ====== COMMAND: /gen ======
@bot.tree.command(name="gen", description="Generate laporan Added/Updated file dari Google Drive")
async def gen(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    drive_service = get_drive_service()

    results = drive_service.files().list(
        q=f"'{FOLDER_ID}' in parents and name contains '.zip'",
        fields="files(id, name, createdTime, modifiedTime, webViewLink)"
    ).execute()
    files = results.get("files", [])

    if not files:
        await interaction.followup.send("⚠️ Tidak ada file .zip di Google Drive.")
        return

    embed_list = []
    global known_files

    for file in files:
        file_id = file["id"]
        name = file["name"]
        created = datetime.datetime.fromisoformat(file["createdTime"][:-1])
        modified = datetime.datetime.fromisoformat(file["modifiedTime"][:-1])
        link = file["webViewLink"]

        status = "🆕 Added"
        if file_id in known_files:
            if modified > known_files[file_id]["modified"]:
                status = "♻️ Updated"
            else:
                continue

        known_files[file_id] = {"modified": modified}

        embed = discord.Embed(
            title=f"{status}: {name}",
            description=f"[🔗 Open File]({link})",
            color=discord.Color.green() if status == "🆕 Added" else discord.Color.orange()
        )
        embed.add_field(name="Upload date", value=created.strftime("%Y-%m-%d"), inline=True)
        embed.add_field(name="Updated date", value=modified.strftime("%Y-%m-%d"), inline=True)
        embed.set_footer(text="Generated via Google Drive API")
        embed_list.append(embed)

    if not embed_list:
        await interaction.followup.send("✅ Tidak ada file baru/updated.")
    else:
        for embed in embed_list:
            await interaction.followup.send(embed=embed)

# ====== START ======
keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))
