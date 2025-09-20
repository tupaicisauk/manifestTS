import os
import discord
import aiohttp
from discord import app_commands
from discord.ext import commands
from flask import Flask
from threading import Thread
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ====================== KEEP ALIVE SERVER ======================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# ====================== DISCORD BOT ======================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# --- Variabel Env ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
FOLDER_ID = os.getenv("FOLDER_ID")
GDRIVE_CREDENTIALS = os.getenv("GDRIVE_CREDENTIALS")

if not DISCORD_TOKEN or not DISCORD_CHANNEL_ID:
    raise ValueError("❌ Pastikan DISCORD_TOKEN & DISCORD_CHANNEL_ID sudah diset.")

# ====================== GOOGLE DRIVE SETUP ======================
import json
creds_dict = json.loads(GDRIVE_CREDENTIALS)
creds = service_account.Credentials.from_service_account_info(
    creds_dict,
    scopes=["https://www.googleapis.com/auth/drive.readonly"]
)
drive_service = build("drive", "v3", credentials=creds)

# ====================== HELPER: Ambil Info Game dari Steam ======================
async def fetch_steam_info(appid: str
