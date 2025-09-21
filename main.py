import os
import io
import zipfile
import discord
from discord.ext import commands
from flask import Flask
from threading import Thread
from google.oauth2 import service_account
from googleapiclient.discovery import build
import aiohttp

# ===== KEEP ALIVE =====
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# ===== DISCORD BOT =====
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))
FOLDER_ID = os.getenv("FOLDER_ID")

if not DISCORD_TOKEN or not DISCORD_CHANNEL_ID or not FOLDER_ID:
    raise ValueError("❌ Pastikan DISCORD_TOKEN, DISCORD_CHANNEL_ID, dan FOLDER_ID sudah di-set!")

# ===== GOOGLE DRIVE API =====
def get_drive_service():
    creds_dict = eval(os.getenv("GDRIVE_CREDENTIALS"))
    creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds)

async def fetch_file_from_drive(filename: str):
    service = get_drive_service()
    results = service.files().list(
        q=f"'{FOLDER_ID}' in parents and name='{filename}'",
        fields="files(id, name, createdTime, modifiedTime, mimeType)"
    ).execute()

    items = results.get("files", [])
    if not items:
        return None, None, None, None

    file_id = items[0]["id"]
    request = service.files().get_media(fileId=file_id)
    data = io.BytesIO(request.execute())
    return data, items[0]["name"], items[0]["createdTime"], items[0]["modifiedTime"]

# ===== STEAM API =====
async def fetch_steam_info(appid: str):
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            if not data or not data[str(appid)]["success"]:
                return None
            game_data = data[str(appid)]["data"]
            return {
                "name": game_data["name"],
                "steam_url": f"https://store.steampowered.com/app/{appid}",
                "steamdb_url": f"https://steamdb.info/app/{appid}/",
                "header_img": game_data["header_image"]
            }

# ===== DISCORD COMMAND =====
@bot.hybrid_command(name="gen", description="Generate manifest dari Google Drive (contoh: /gen 1086940)")
async def gen(ctx, appid: str):
    await ctx.defer()

    filename = f"{appid}.zip"
    buffer, name, created, modified = await fetch_file_from_drive(filename)

    if not buffer:
        await ctx.reply(f"❌ Manifest {filename} tidak ditemukan di Google Drive.")
        return

    # Periksa isi ZIP
    buffer.seek(0)
    z = zipfile.ZipFile(buffer)
    dlc_files = [f for f in z.namelist() if "dlc" in f.lower()]
    total_dlc = len(dlc_files)

    # Ambil info Steam
    steam_info = await fetch_steam_info(appid)

    embed = discord.Embed(
        title=f"✅ Manifest Generated: {steam_info['name'] if steam_info else filename}",
        description=f"Successfully generated manifest files for **{steam_info['name'] if steam_info else filename}** ({appid})",
        color=discord.Color.green()
    )

    if steam_info:
        embed.add_field(name="Links", value=f"[Steam Store]({steam_info['steam_url']}) | [SteamDB]({steam_info['steamdb_url']})", inline=False)

    embed.add_field(name="Manifest Status", value="✅ Manifest ditemukan di Google Drive", inline=False)
    embed.add_field(name="DLC Status", value=f"✅ Total DLC: {total_dlc}\nDetected Files: {', '.join(dlc_files) if dlc_files else 'Tidak ada'}", inline=False)
    embed.add_field(name="Google Drive", value=f"**Upload date:** {created[:10]}\n**Updated date:** {modified[:10]}", inline=False)

    if steam_info and steam_info.get("header_img"):
        embed.set_image(url=steam_info["header_img"])

    buffer.seek(0)
    file = discord.File(buffer, filename=filename)

    await ctx.reply(embed=embed, file=file)

# ===== START =====
@bot.event
async def on_ready():
    synced = await bot.tree.sync()
    print(f"✅ Bot {bot.user} sudah online! Slash commands: {len(synced)}")

keep_alive()
bot.run(DISCORD_TOKEN)
