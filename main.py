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
async def fetch_steam_info(appid: str):
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=us&l=en"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if not data or not data.get(appid, {}).get("success"):
                return None
            game_data = data[appid]["data"]
            return {
                "name": game_data.get("name", "Unknown Game"),
                "header_image": game_data.get("header_image", ""),
                "steam_url": f"https://store.steampowered.com/app/{appid}",
                "steamdb_url": f"https://steamdb.info/app/{appid}"
            }

# ====================== DISCORD COMMAND: /gen ======================
@bot.tree.command(name="gen", description="Generate manifest berdasarkan AppID")
@app_commands.describe(appid="AppID game (contoh: 1086940)")
async def gen(interaction: discord.Interaction, appid: str):
    await interaction.response.defer()  # biar ga timeout

    # --- Cari file di Google Drive ---
    query = f"'{FOLDER_ID}' in parents and name contains '{appid}.zip'"
    results = drive_service.files().list(
        q=query,
        fields="files(id, name, createdTime, modifiedTime, webViewLink)",
        pageSize=1
    ).execute()
    files = results.get("files", [])

    if not files:
        await interaction.followup.send(f"❌ Manifest untuk AppID `{appid}` tidak ditemukan di Google Drive.")
        return

    file = files[0]

    # --- Ambil info game dari Steam ---
    steam_info = await fetch_steam_info(appid)
    if not steam_info:
        game_name = f"AppID {appid}"
        cover_url = ""
        steam_url = f"https://store.steampowered.com/app/{appid}"
        steamdb_url = f"https://steamdb.info/app/{appid}"
    else:
        game_name = steam_info["name"]
        cover_url = steam_info["header_image"]
        steam_url = steam_info["steam_url"]
        steamdb_url = steam_info["steamdb_url"]

    # --- Embed hasil ---
    embed = discord.Embed(
        title=f"✅ Manifest Generated: {game_name}",
        description=f"Successfully generated manifest files for **{game_name}** (`{appid}`)",
        color=discord.Color.green()
    )
    embed.add_field(name="Links", value=f"[Steam Store]({steam_url}) | [SteamDB]({steamdb_url})", inline=False)
    embed.add_field(
        name="Manifest Status",
        value="✅ Manifest ditemukan di Google Drive",
        inline=False
    )
    embed.add_field(
        name="DLC Status",
        value=(
            "✅ **Total DLC:** Data DLC belum dihubungkan\n"
            "Existing: ? | Missing: ?\n"
            "Completion: ?"
        ),
        inline=False
    )
    embed.add_field(
        name="Google Drive",
        value=f"[Open File]({file['webViewLink']})\n"
              f"Upload date: {file['createdTime'][:10]}\n"
              f"Updated date: {file['modifiedTime'][:10]}",
        inline=False
    )
    if cover_url:
        embed.set_image(url=cover_url)
    embed.set_footer(text="Generated via Google Drive + Steam API")

    await interaction.followup.send(embed=embed)

# ====================== START BOT ======================
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✅ Slash commands synced: {len(synced)} commands")
    except Exception as e:
        print(f"⚠️ Error syncing commands: {e}")
    print(f"✅ Bot {bot.user} sudah online!")

keep_alive()
bot.run(DISCORD_TOKEN)
